"""App Tasks"""

# Standard Library
import logging
import requests
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Django
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import AppRegistryNotReady, ImproperlyConfigured
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

# Third Party
from celery import shared_task

# Alliance Auth
from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo, EveAllianceInfo

# AA Campaign
from .models import Campaign, CampaignKillmail, CampaignMember, CampaignTarget
from .esi import ESIHandler
from .eve_sde import (
    get_constellation_model,
    get_item_type_model,
    get_region_model,
    get_related_field_name,
    get_solar_system_model,
)

logger = logging.getLogger(__name__)

EveSolarSystem = None
EveConstellation = None
EveRegion = None
EveType = None
_SYSTEM_CONSTELLATION_FIELD = None
_CONSTELLATION_REGION_FIELD = None
_TYPE_GROUP_FIELD = None


def _ensure_eve_models():
    global EveSolarSystem, EveConstellation, EveRegion, EveType
    if EveSolarSystem is None:
        EveSolarSystem = get_solar_system_model()
        EveConstellation = get_constellation_model()
        EveRegion = get_region_model()
        EveType = get_item_type_model()
    return EveSolarSystem, EveConstellation, EveRegion, EveType


def _ensure_eve_fields():
    global _SYSTEM_CONSTELLATION_FIELD, _CONSTELLATION_REGION_FIELD, _TYPE_GROUP_FIELD
    _ensure_eve_models()
    if _SYSTEM_CONSTELLATION_FIELD is None:
        _SYSTEM_CONSTELLATION_FIELD = get_related_field_name(
            EveSolarSystem, ("constellation", "eve_constellation")
        )
        if not _SYSTEM_CONSTELLATION_FIELD:
            raise ImproperlyConfigured(
                "Solar system model is missing a constellation relation. "
                "Expected one of: constellation, eve_constellation."
            )
    if _CONSTELLATION_REGION_FIELD is None:
        _CONSTELLATION_REGION_FIELD = get_related_field_name(
            EveConstellation, ("region", "eve_region")
        )
        if not _CONSTELLATION_REGION_FIELD:
            raise ImproperlyConfigured(
                "Constellation model is missing a region relation. "
                "Expected one of: region, eve_region."
            )
    if _TYPE_GROUP_FIELD is None:
        _TYPE_GROUP_FIELD = get_related_field_name(
            EveType, ("group", "item_group", "eve_group")
        )
    return _SYSTEM_CONSTELLATION_FIELD, _CONSTELLATION_REGION_FIELD, _TYPE_GROUP_FIELD


def _system_select_related_fields():
    constellation_field, region_field, _ = _ensure_eve_fields()
    fields = [constellation_field]
    if region_field:
        fields.append(f"{constellation_field}__{region_field}")
    return fields


def _get_system_constellation_id(system):
    constellation_field, _, _ = _ensure_eve_fields()
    return getattr(system, f"{constellation_field}_id", None)


def _get_system_region_id(system):
    constellation_field, region_field, _ = _ensure_eve_fields()
    constellation = getattr(system, constellation_field, None)
    if not constellation:
        return None
    return getattr(constellation, f"{region_field}_id", None)


def _get_item_type(ship_type_id, context=None):
    if not ship_type_id:
        return None

    _ensure_eve_models()
    _, _, _, group_field = _ensure_eve_fields()
    if context and ship_type_id in context.get("resolved_types", {}):
        return context["resolved_types"][ship_type_id]

    qs = EveType.objects.filter(id=ship_type_id)
    if group_field:
        qs = qs.select_related(group_field)
    s_type = qs.first()

    if s_type is None and hasattr(EveType.objects, "get_or_create_esi"):
        try:
            s_type, _ = EveType.objects.get_or_create_esi(id=ship_type_id)
        except Exception:
            s_type = None

    if context is not None:
        context.setdefault("resolved_types", {})[ship_type_id] = s_type
    return s_type


def _get_type_group_name(s_type):
    if not s_type:
        return "Unknown"
    for attr in ("eve_group", "group", "item_group"):
        group = getattr(s_type, attr, None)
        if group and getattr(group, "name", None):
            return group.name
    return "Unknown"


try:
    _ensure_eve_models()
except AppRegistryNotReady:
    pass

# Reusable session for zKillboard calls
_zkill_session = requests.Session()
_zkill_retries = Retry(
    total=3,
    backoff_factor=2,
    status_forcelist=[429, 500, 502, 503, 504]
)
_zkill_session.mount('https://', HTTPAdapter(max_retries=_zkill_retries))

_last_zkill_call = 0


def _zkill_get(url):
    """
    Helper to perform GET requests to zKillboard with rate limiting.
    Enforces a minimum of 500ms between calls.
    """
    global _last_zkill_call
    now = time.time()
    elapsed = now - _last_zkill_call
    if elapsed < 0.5:
        sleep_time = 0.5 - elapsed
        time.sleep(sleep_time)

    contact_email = getattr(settings, 'ESI_USER_CONTACT_EMAIL', 'Unknown')
    headers = {
        'User-Agent': f'Alliance Auth Campaign Plugin - Maintainer: {contact_email}',
        'Accept-Encoding': 'gzip',
    }

    logger.debug(f"Fetching from zKillboard: {url}")
    response = _zkill_session.get(url, headers=headers, timeout=30)
    _last_zkill_call = time.time()
    return response


def _fetch_universe_names(ids):
    try:
        data = ESIHandler.post_universe_names(ids, use_etag=False)
        return data
    except Exception:
        return None


def get_killmail_data_from_db(killmail_id):
    """
    Try to find killmail data in our database from previous campaign matches.
    Returns (killmail_time, solar_system_id) or (None, None)
    """
    existing = CampaignKillmail.objects.filter(killmail_id=killmail_id).first()
    if existing:
        return existing.killmail_time, existing.solar_system_id
    return None, None


@shared_task(time_limit=7200)
def pull_zkillboard_data(past_seconds=None):
    """
    Pull data from ZKillboard for all active campaigns.
    Recommended to be scheduled hourly.
    """
    lock_id = "aacampaign-pull-zkillboard-data-lock"
    # Acquire lock for 2 hours (7200s) as a hard limit.
    if not cache.add(lock_id, True, 7200):
        logger.warning("ZKillboard data pull task is already running. Skipping this run.")
        return "Task already running"

    try:
        return _pull_zkillboard_data_logic(lock_id, past_seconds)
    finally:
        cache.delete(lock_id)

def _pull_zkillboard_data_logic(lock_id, past_seconds=None):
    _ensure_eve_models()
    logger.info("ZKillboard data pull task started")
    start_time = time.time()
    now = timezone.now()
    twelve_hours_ago = now - timezone.timedelta(hours=12)
    active_campaigns = list(Campaign.objects.filter(
        is_active=True
    ).filter(
        Q(end_date__isnull=True) | Q(end_date__gt=twelve_hours_ago)
    ).prefetch_related('members', 'targets', 'systems', 'constellations', 'regions'))

    if not active_campaigns:
        logger.info("No active campaigns to process")
        return "No active campaigns"

    # Pre-calculate campaign metadata to avoid redundant DB queries
    campaign_meta = {}
    for campaign in active_campaigns:
        campaign_meta[campaign.id] = {
            'friendly_ids': get_campaign_friendly_ids(campaign),
            'target_ids': get_campaign_target_ids(campaign),
            'system_ids': set(campaign.systems.values_list('id', flat=True)),
            'constellation_ids': set(campaign.constellations.values_list('id', flat=True)),
            'region_ids': set(campaign.regions.values_list('id', flat=True)),
        }

    # Local caches for the duration of the task
    context = {
        'resolved_names': {},
        'resolved_characters': {},
        'resolved_systems': {},
        'resolved_types': {},
    }

    # Collect all unique entities to pull for and their required lookback
    raw_entities = {} # (entity_type, entity_id) -> min_start_date
    for campaign in active_campaigns:
        # Determine how far back we need to pull for this campaign
        if past_seconds:
            # Explicit override
            campaign_lookback = now - timezone.timedelta(seconds=past_seconds)
        elif campaign.last_run is None:
            # New campaign: pull from start_date
            campaign_lookback = campaign.start_date
            logger.info(f"Campaign {campaign.name} is new or never pulled, pulling from {campaign_lookback}")
        else:
            # Established campaign: pull from last 3 hours
            # We use 3 hours to have some overlap and ensure no gaps if the task was slightly delayed.
            campaign_lookback = now - timezone.timedelta(hours=3)
            logger.debug(f"Campaign {campaign.name} is established, pulling from {campaign_lookback}")

        # Never look back before the campaign actually started
        if campaign_lookback < campaign.start_date:
            campaign_lookback = campaign.start_date

        friendly_ids = campaign_meta[campaign.id]['friendly_ids']
        target_ids = campaign_meta[campaign.id]['target_ids']
        has_friendlies = any(friendly_ids.values())
        has_filters = (
            target_ids['characters'] or
            target_ids['corporations'] or
            target_ids['alliances'] or
            target_ids['factions'] or
            campaign_meta[campaign.id]['system_ids'] or
            campaign_meta[campaign.id]['constellation_ids'] or
            campaign_meta[campaign.id]['region_ids']
        )

        def add_raw_entity(etype, eid):
            if (etype, eid) not in raw_entities or campaign_lookback < raw_entities[(etype, eid)]:
                raw_entities[(etype, eid)] = campaign_lookback

        if has_friendlies:
            # Friendly-first pull: reduces zKillboard volume and ESI calls.
            for char_id in friendly_ids['characters']:
                add_raw_entity('characterID', char_id)
            for corp_id in friendly_ids['corporations']:
                add_raw_entity('corporationID', corp_id)
            for alliance_id in friendly_ids['alliances']:
                add_raw_entity('allianceID', alliance_id)
            for faction_id in friendly_ids['factions']:
                add_raw_entity('factionID', faction_id)
        elif has_filters:
            # No friendlies configured; fall back to targets and locations.
            for target in campaign.targets.all():
                if target.character: add_raw_entity('characterID', target.character.character_id)
                if target.corporation: add_raw_entity('corporationID', target.corporation.corporation_id)
                if target.alliance: add_raw_entity('allianceID', target.alliance.alliance_id)
                if target.faction: add_raw_entity('factionID', target.faction.faction_id)

            for system in campaign.systems.all(): add_raw_entity('systemID', system.id)
            for constellation in campaign.constellations.all(): add_raw_entity('constellationID', constellation.id)
            for region in campaign.regions.all(): add_raw_entity('regionID', region.id)

    if not raw_entities:
        Campaign.objects.filter(id__in=[c.id for c in active_campaigns]).update(last_run=now)
        logger.info(f"No entities found to pull for in {len(active_campaigns)} active campaigns")
        return "No entities found"

    # Hierarchy De-duplication to reduce redundant API calls
    # E.g. if we pull an Alliance, we don't need to pull its Corporations if they have the same or shorter lookback.
    entities = {} # (etype, eid) -> start_date
    by_type = {}
    for (etype, eid), start_date in raw_entities.items():
        by_type.setdefault(etype, {})[eid] = start_date

    # 1. De-duplicate characters (Skip if their corp or alliance is also being pulled with sufficient range)
    char_ids = list(by_type.get('characterID', {}).keys())
    char_info = {c.character_id: (c.corporation_id, c.alliance_id) for c in EveCharacter.objects.filter(character_id__in=char_ids)}
    for eid, start_date in by_type.get('characterID', {}).items():
        corp_id, alliance_id = char_info.get(eid, (None, None))
        parent_being_pulled = False
        if corp_id and corp_id in by_type.get('corporationID', {}):
            if by_type['corporationID'][corp_id] <= start_date:
                parent_being_pulled = True
        if alliance_id and alliance_id in by_type.get('allianceID', {}):
            if by_type['allianceID'][alliance_id] <= start_date:
                parent_being_pulled = True

        if not parent_being_pulled:
            entities[('characterID', eid)] = start_date

    # 2. De-duplicate corporations (Skip if their alliance is also being pulled with sufficient range)
    corp_ids = list(by_type.get('corporationID', {}).keys())
    corp_info = {c.corporation_id: c.alliance.alliance_id if c.alliance else None
                 for c in EveCorporationInfo.objects.filter(corporation_id__in=corp_ids).select_related('alliance')}
    for eid, start_date in by_type.get('corporationID', {}).items():
        alliance_eve_id = corp_info.get(eid)
        parent_being_pulled = False
        if alliance_eve_id and alliance_eve_id in by_type.get('allianceID', {}):
            if by_type['allianceID'][alliance_eve_id] <= start_date:
                parent_being_pulled = True

        if not parent_being_pulled:
            entities[('corporationID', eid)] = start_date

    # 3. De-duplicate systems (Skip if constellation or region is being pulled with sufficient range)
    system_ids = list(by_type.get('systemID', {}).keys())
    system_qs = EveSolarSystem.objects.filter(id__in=system_ids)
    select_related_fields = _system_select_related_fields()
    if select_related_fields:
        system_qs = system_qs.select_related(*select_related_fields)
    system_info = {
        s.id: (_get_system_constellation_id(s), _get_system_region_id(s))
        for s in system_qs
    }
    for eid, start_date in by_type.get('systemID', {}).items():
        const_id, region_id = system_info.get(eid, (None, None))
        parent_being_pulled = False
        if const_id and const_id in by_type.get('constellationID', {}):
            if by_type['constellationID'][const_id] <= start_date:
                parent_being_pulled = True
        if region_id and region_id in by_type.get('regionID', {}):
            if by_type['regionID'][region_id] <= start_date:
                parent_being_pulled = True

        if not parent_being_pulled:
            entities[('systemID', eid)] = start_date

    # 4. De-duplicate constellations (Skip if region is being pulled with sufficient range)
    const_ids = list(by_type.get('constellationID', {}).keys())
    _, region_field, _ = _ensure_eve_fields()
    const_info = {
        c.id: getattr(c, f"{region_field}_id", None)
        for c in EveConstellation.objects.filter(id__in=const_ids)
    }
    for eid, start_date in by_type.get('constellationID', {}).items():
        region_id = const_info.get(eid)
        parent_being_pulled = False
        if region_id and region_id in by_type.get('regionID', {}):
            if by_type['regionID'][region_id] <= start_date:
                parent_being_pulled = True

        if not parent_being_pulled:
            entities[('constellationID', eid)] = start_date

    # Add all Alliances and Regions as they are top-level
    for eid, start_date in by_type.get('allianceID', {}).items():
        entities[('allianceID', eid)] = start_date
    for eid, start_date in by_type.get('regionID', {}).items():
        entities[('regionID', eid)] = start_date
    for eid, start_date in by_type.get('factionID', {}).items():
        entities[('factionID', eid)] = start_date

    skipped_count = len(raw_entities) - len(entities)
    logger.info(f"Entities to pull: {len(entities)} (Optimized/Skipped {skipped_count} redundant entities)")

    # Pull killmails for each entity and process them
    processed_ids = set()
    campaign_killmails_count = 0

    def process_page_of_kms(kms):
        nonlocal campaign_killmails_count
        km_ids = [km.get('killmail_id') for km in kms if km.get('killmail_id')]

        # Batch pre-resolve systems for this page
        system_ids = {km['solar_system_id'] for km in kms if km.get('solar_system_id')}
        missing_system_ids = system_ids - set(context['resolved_systems'].keys())
        if missing_system_ids:
            new_systems = EveSolarSystem.objects.filter(id__in=missing_system_ids)
            select_related_fields = _system_select_related_fields()
            if select_related_fields:
                new_systems = new_systems.select_related(*select_related_fields)
            for s in new_systems:
                context['resolved_systems'][s.id] = s

        # Batch check existing killmails for all active campaigns
        existing_map = {} # km_id -> set of campaign_ids
        existing_qs = CampaignKillmail.objects.filter(
            killmail_id__in=km_ids,
            campaign__in=active_campaigns
        ).values_list('killmail_id', 'campaign_id')
        for kid, cid in existing_qs:
            existing_map.setdefault(kid, set()).add(cid)

        new_on_page = 0
        for km in kms:
            km_id = km.get('killmail_id')
            if km_id and km_id not in processed_ids:
                processed_ids.add(km_id)

                existing_campaign_ids = existing_map.get(km_id, set())
                campaigns_to_check = [c for c in active_campaigns if c.id not in existing_campaign_ids]

                if not campaigns_to_check:
                    continue

                processed_for_any = False
                for campaign in campaigns_to_check:
                    if should_include_killmail(campaign, km, campaign_meta, context):
                        process_killmail(campaign, km, campaign_meta, context)
                        campaign_killmails_count += 1
                        processed_for_any = True

                if processed_for_any:
                    new_on_page += 1
        return new_on_page

    total_entities = len(entities)
    for i, ((entity_type, entity_id), min_start_date) in enumerate(entities.items(), 1):
        # Hard stop if task exceeded 2 hours
        if time.time() - start_time > 7200:
            logger.warning("Task exceeded 2 hour limit, stopping early.")
            break

        seconds_to_pull = int((now - min_start_date).total_seconds())
        logger.info(f"[{i}/{total_entities}] Discovery for {entity_type} {entity_id} from {min_start_date} ({seconds_to_pull}s ago)")

        if seconds_to_pull < 172800: # 48 hours
            # Use pastSeconds API for recent pulls - it's much faster
            page = 1
            consecutive_errors = 0
            max_consecutive_errors = 3
            while page <= 20: # Should be plenty
                kms = fetch_from_zkill(entity_type, entity_id, past_seconds=seconds_to_pull, page=page)
                if kms is None:
                    consecutive_errors += 1
                    logger.warning(
                        f"Failed to fetch page {page} for pastSeconds on {entity_type} {entity_id}. "
                        f"Skipping page ({consecutive_errors}/{max_consecutive_errors})."
                    )
                    if consecutive_errors >= max_consecutive_errors:
                        logger.warning(
                            f"Too many consecutive errors for {entity_type} {entity_id}. Stopping killmail pull."
                        )
                        break
                    page += 1
                    continue

                consecutive_errors = 0
                if not kms:
                    break

                logger.info(f"Fetched page {page} ({len(kms)} kills) for {entity_type} {entity_id}")
                new_on_page = process_page_of_kms(kms)
                logger.info(f"Processed {new_on_page} unique killmails from page {page}")

                if len(kms) < 1000: # Last page
                    break

                # Check if last km on page is older than min_start_date
                last_km_time = get_killmail_time(kms[-1])
                if last_km_time and last_km_time < min_start_date:
                    break

                page += 1
        else:
            # Historical pull using year/month loop
            reached_min_date = False
            curr_now = now
            curr_year = curr_now.year
            curr_month = curr_now.month
            start_year = min_start_date.year
            start_month = min_start_date.month

            while (curr_year > start_year) or (curr_year == start_year and curr_month >= start_month):
                page = 1
                max_pages_per_month = 50
                logger.debug(f"Pulling {entity_type} {entity_id} for {curr_year}-{curr_month:02d}")

                consecutive_errors = 0
                max_consecutive_errors = 3
                while page <= max_pages_per_month:
                    kms = fetch_from_zkill(entity_type, entity_id, page=page, year=curr_year, month=curr_month)
                    if kms is None:
                        consecutive_errors += 1
                        logger.warning(
                            f"Failed to fetch page {page} for {curr_year}-{curr_month:02d}. "
                            f"Skipping page ({consecutive_errors}/{max_consecutive_errors})."
                        )
                        if consecutive_errors >= max_consecutive_errors:
                            logger.warning(
                                f"Too many consecutive errors for {entity_type} {entity_id} "
                                f"({curr_year}-{curr_month:02d}). Skipping month."
                            )
                            break
                        page += 1
                        continue

                    consecutive_errors = 0

                    if not kms:
                        logger.debug(f"No more killmails for {curr_year}-{curr_month:02d} at page {page}")
                        break

                    logger.info(f"Fetched page {page} ({len(kms)} kills) for {entity_type} {entity_id} ({curr_year}-{curr_month:02d})")
                    new_on_page = process_page_of_kms(kms)
                    logger.info(f"Processed {new_on_page} unique killmails from page {page}")

                    # Check if we should continue paging this month
                    last_km_time = get_killmail_time(kms[-1])
                    if last_km_time and last_km_time < min_start_date:
                        reached_min_date = True
                        break

                    page += 1

                if reached_min_date:
                    logger.info(f"Reached data older than {min_start_date}. Stopping for {entity_type} {entity_id}.")
                    break

                if page > max_pages_per_month:
                    logger.warning(f"Reached max pages ({max_pages_per_month}) for {curr_year}-{curr_month:02d}. Moving to next month.")

                # Decrement month
                curr_month -= 1
                if curr_month < 1:
                    curr_month = 12
                    curr_year -= 1

    # Update last_run for all campaigns processed
    Campaign.objects.filter(id__in=[c.id for c in active_campaigns]).update(last_run=now)

    logger.info(f"Finished pulling ZKillboard data. Processed {campaign_killmails_count} campaign killmails. Task completed successfully.")
    return f"Processed {campaign_killmails_count} campaign killmails"

@shared_task(time_limit=7200)
def repair_campaign_killmails():
    """
    Find killmails with missing information and attempt to repair them
    by fetching full data from zKillboard and ESI.
    """
    lock_id = "aacampaign-repair-campaign-killmails-lock"
    # Acquire lock for 2 hours (7200s) as a hard limit.
    if not cache.add(lock_id, True, 7200):
        logger.warning("Repair task is already running. Skipping.")
        return "Task already running"

    try:
        # Get unique killmail IDs that need repair
        kms_to_repair = list(CampaignKillmail.objects.filter(
            Q(ship_type_id=0) |
            Q(ship_type_name="Unknown", ship_type_id__gt=0) |
            Q(ship_group_name="Unknown") |
            Q(victim_name="Unknown", victim_id__gt=0) |
            Q(victim_corp_name="Unknown", victim_corp_id__gt=0) |
            Q(final_blow_char_id=0, final_blow_corp_id=0) |
            Q(final_blow_char_name="", final_blow_char_id__gt=0) |
            Q(final_blow_char_name="Unknown", final_blow_char_id__gt=0) |
            Q(final_blow_corp_name="Unknown", final_blow_corp_id__gt=0)
        ).values_list('killmail_id', flat=True).distinct())

        if not kms_to_repair:
            logger.info("No killmails found in need of repair")
            return "No killmails to repair"

        total = len(kms_to_repair)
        logger.info(f"Repairing {total} killmails with missing information")

        active_campaigns = list(Campaign.objects.filter(is_active=True).prefetch_related('members', 'targets', 'systems', 'constellations', 'regions'))
        campaign_meta = {}
        for campaign in active_campaigns:
            campaign_meta[campaign.id] = {
                'friendly_ids': get_campaign_friendly_ids(campaign),
                'target_ids': get_campaign_target_ids(campaign),
                'system_ids': set(campaign.systems.values_list('id', flat=True)),
                'constellation_ids': set(campaign.constellations.values_list('id', flat=True)),
                'region_ids': set(campaign.regions.values_list('id', flat=True)),
            }

        context = {
            'resolved_names': {},
            'resolved_characters': {},
            'resolved_systems': {},
            'resolved_types': {},
        }

        repaired_count = 0
        start_time = time.time()
        for i, km_id in enumerate(kms_to_repair, 1):
            # Hard stop if task exceeded 2 hours
            if time.time() - start_time > 7200:
                logger.warning("Repair task exceeded 2 hour limit, stopping early.")
                break

            if repair_killmail_by_id(km_id, campaign_meta, context):
                repaired_count += 1
            if i % 10 == 0:
                logger.info(f"Processed {i}/{total} killmails (Repaired: {repaired_count})")

        logger.info(f"Finished repair. Successfully repaired {repaired_count} killmails.")
        return f"Repaired {repaired_count} killmails"
    finally:
        cache.delete(lock_id)

def repair_killmail_by_id(km_id, campaign_meta=None, context=None):
    """
    Finds a killmail on zKillboard and processes it for all relevant campaigns.
    Returns True if found and processed, False otherwise.
    """
    url = f"https://zkillboard.com/api/killID/{km_id}/"
    try:
        response = _zkill_get(url)
        data = response.json()
        if isinstance(data, list) and len(data) > 0:
            km_data = data[0]
            # should_include_killmail will fetch from ESI because it's missing 'victim'
            # but it needs a campaign. We iterate over all campaigns this killmail belongs to.
            campaigns = Campaign.objects.filter(killmails__killmail_id=km_id).distinct()
            repaired = False
            for campaign in campaigns:
                if should_include_killmail(campaign, km_data, campaign_meta, context):
                    process_killmail(campaign, km_data, campaign_meta, context)
                    repaired = True
                else:
                    logger.debug(f"Killmail {km_id} does not match campaign {campaign} anymore during repair")
            return repaired
        else:
            logger.warning(f"Could not find killmail {km_id} on zKillboard for repair")
    except Exception as e:
        logger.error(f"Error repairing killmail {km_id}: {e}")
    return False

@shared_task(time_limit=7200)
def cleanup_campaign_killmails(campaign_id=None):
    """
    Remove campaign killmails that no longer match current campaign rules.
    Optionally limit cleanup to a single campaign by ID.
    """
    lock_id = "aacampaign-cleanup-campaign-killmails-lock"
    # Acquire lock for 2 hours (7200s) as a hard limit.
    if not cache.add(lock_id, True, 7200):
        logger.warning("Cleanup task is already running. Skipping.")
        return "Task already running"

    try:
        killmail_qs = CampaignKillmail.objects.all()
        if campaign_id:
            killmail_qs = killmail_qs.filter(campaign_id=campaign_id)

        km_ids = list(killmail_qs.values_list('killmail_id', flat=True).distinct())
        if not km_ids:
            logger.info("No killmails found to clean up")
            return "No killmails to clean up"

        campaign_ids = list(killmail_qs.values_list('campaign_id', flat=True).distinct())
        campaigns = list(Campaign.objects.filter(id__in=campaign_ids).prefetch_related(
            'members', 'targets', 'systems', 'constellations', 'regions'
        ))
        campaign_meta = {}
        for campaign in campaigns:
            campaign_meta[campaign.id] = {
                'friendly_ids': get_campaign_friendly_ids(campaign),
                'target_ids': get_campaign_target_ids(campaign),
                'system_ids': set(campaign.systems.values_list('id', flat=True)),
                'constellation_ids': set(campaign.constellations.values_list('id', flat=True)),
                'region_ids': set(campaign.regions.values_list('id', flat=True)),
            }

        context = {
            'resolved_names': {},
            'resolved_characters': {},
            'resolved_systems': {},
            'resolved_types': {},
        }

        removed_total = 0
        kept_total = 0
        skipped_total = 0
        start_time = time.time()
        total = len(km_ids)
        allowed_campaign_ids = set(campaign_ids)

        for i, km_id in enumerate(km_ids, 1):
            # Hard stop if task exceeded 2 hours
            if time.time() - start_time > 7200:
                logger.warning("Cleanup task exceeded 2 hour limit, stopping early.")
                break

            removed, kept, skipped = cleanup_killmail_by_id(
                km_id,
                campaign_meta=campaign_meta,
                context=context,
                allowed_campaign_ids=allowed_campaign_ids
            )
            removed_total += removed
            kept_total += kept
            skipped_total += skipped
            if i % 10 == 0:
                logger.info(
                    f"Processed {i}/{total} killmails "
                    f"(Removed: {removed_total}, Kept: {kept_total}, Skipped: {skipped_total})"
                )

        logger.info(
            f"Cleanup complete. Removed {removed_total} campaign killmails "
            f"(Kept: {kept_total}, Skipped: {skipped_total})."
        )
        return f"Cleanup complete. Removed {removed_total} campaign killmails"
    finally:
        cache.delete(lock_id)

def cleanup_killmail_by_id(km_id, campaign_meta=None, context=None, allowed_campaign_ids=None):
    """
    Re-evaluate a killmail against its campaigns and remove mismatches.
    Returns (removed, kept, skipped).
    """
    url = f"https://zkillboard.com/api/killID/{km_id}/"
    try:
        response = _zkill_get(url)
        data = response.json()
        if isinstance(data, list) and len(data) > 0:
            km_data = data[0]
            campaigns = Campaign.objects.filter(killmails__killmail_id=km_id).distinct()
            if allowed_campaign_ids:
                campaigns = campaigns.filter(id__in=allowed_campaign_ids)

            removed = 0
            kept = 0
            skipped = 0
            for campaign in campaigns:
                match = should_include_killmail(
                    campaign,
                    km_data,
                    campaign_meta,
                    context,
                    allow_incomplete=True
                )
                if match is True:
                    process_killmail(campaign, km_data, campaign_meta, context)
                    kept += 1
                elif match is False:
                    CampaignKillmail.objects.filter(campaign=campaign, killmail_id=km_id).delete()
                    removed += 1
                    logger.info(f"Killmail {km_id} removed from campaign {campaign}: no longer matches")
                else:
                    skipped += 1
                    logger.debug(f"Killmail {km_id} skipped for campaign {campaign}: incomplete data during cleanup")
            return removed, kept, skipped
        else:
            logger.warning(f"Could not find killmail {km_id} on zKillboard for cleanup")
    except Exception as e:
        logger.error(f"Error cleaning killmail {km_id}: {e}")
    return 0, 0, 0

def fetch_from_zkill(entity_type, entity_id, past_seconds=None, page=None, year=None, month=None):
    if past_seconds:
        url = f"https://zkillboard.com/api/{entity_type}/{entity_id}/pastSeconds/{past_seconds}/"
    else:
        url = f"https://zkillboard.com/api/{entity_type}/{entity_id}/"
        if year and month:
            url += f"year/{year}/month/{month}/"

    if page:
        url += f"page/{page}/"
    else:
        url += "page/1/"

    for attempt in range(1, 4):
        try:
            response = _zkill_get(url)
            if response.status_code != 200:
                raise ValueError(f"Status {response.status_code}")
            data = response.json()
            if not isinstance(data, list):
                logger.error(
                    f"Unexpected response from zKillboard for {entity_type} {entity_id}: "
                    f"expected list, got {type(data)}. Content: {data}"
                )
                return None
            if not data:
                logger.debug(f"No results from zKillboard for {entity_type} {entity_id}")
                return []
            filtered = [km for km in data if isinstance(km, dict)]
            if not filtered:
                logger.debug(f"All results were non-dict from zKillboard for {entity_type} {entity_id}")
                return None
            logger.debug(f"Fetched {len(filtered)} results from zKillboard for {entity_type} {entity_id}")
            return filtered
        except Exception as e:
            if attempt < 3:
                logger.warning(
                    f"Error fetching from zkillboard for {entity_type} {entity_id} (attempt {attempt}/3): {e}"
                )
                time.sleep(2 * attempt)
                continue
            logger.error(f"Error fetching from zkillboard for {entity_type} {entity_id}: {e}")
            return None

def fetch_killmail_from_esi(killmail_id, killmail_hash):
    try:
        logger.debug(f"Fetching killmail {killmail_id} from ESI")
        data = ESIHandler.get_killmail(
            killmail_id=killmail_id,
            killmail_hash=killmail_hash,
            force_refresh=True,
        )
        return data
    except Exception as e:
        logger.error(f"Error fetching killmail {killmail_id} from ESI: {e}")
        return None

def get_killmail_time(km_data):
    # Try to get it from km_data
    km_time_str = km_data.get('killmail_time')
    if km_time_str:
        try:
            km_time = timezone.datetime.fromisoformat(km_time_str.replace('Z', '+00:00'))
            if timezone.is_naive(km_time):
                km_time = timezone.make_aware(km_time)
            return km_time
        except Exception:
            pass

    # Not found, try local DB first
    km_id = km_data.get('killmail_id')
    if km_id:
        db_time, _ = get_killmail_data_from_db(km_id)
        if db_time:
            return db_time

    # Not found in DB, try ESI if we have ID and Hash
    km_hash = km_data.get('zkb', {}).get('hash')
    if km_id and km_hash:
        esi_data = fetch_killmail_from_esi(km_id, km_hash)
        if esi_data:
            km_time_str = esi_data.get('killmail_time')
            if km_time_str:
                try:
                    km_time = timezone.datetime.fromisoformat(km_time_str.replace('Z', '+00:00'))
                    if timezone.is_naive(km_time):
                        km_time = timezone.make_aware(km_time)
                    return km_time
                except Exception:
                    pass
    return None

def should_include_killmail(campaign, km_data, campaign_meta=None, context=None, allow_incomplete=False):
    _ensure_eve_models()
    # Basic validation
    km_id = km_data.get('killmail_id', 'Unknown')

    # Check if we have enough data to evaluate involvement and process it correctly
    # We need: time, system, victim (for ship info), and attackers (for involvement and final blow)
    attacker_count = km_data.get('zkb', {}).get('attackerCount', 0)
    has_all_attackers = 'attackers' in km_data and len(km_data['attackers']) >= attacker_count
    has_final_blow = 'attackers' in km_data and any(a.get('final_blow') for a in km_data['attackers'])
    has_final_blow_char = (
        'attackers' in km_data and
        any(a.get('final_blow') and a.get('character_id') for a in km_data['attackers'])
    )

    needs_esi = (
        any(k not in km_data for k in ['killmail_time', 'solar_system_id', 'victim', 'attackers']) or
        not has_final_blow_char or
        not has_all_attackers
    )

    if needs_esi:
        km_id_val = km_data.get('killmail_id')
        km_hash = km_data.get('zkb', {}).get('hash')

        # Check local DB cache for time/system/victim if that's all we were missing
        # But if we are missing attackers with final blow info or full list, we usually need ESI
        if ('killmail_time' not in km_data or 'solar_system_id' not in km_data):
            if km_id_val:
                db_time, db_system_id = get_killmail_data_from_db(km_id_val)
                if db_time and db_system_id:
                    km_data['killmail_time'] = db_time.isoformat()
                    km_data['solar_system_id'] = db_system_id
                    # Re-check if we still need ESI
                    has_all_attackers = 'attackers' in km_data and len(km_data['attackers']) >= attacker_count
                    has_final_blow = 'attackers' in km_data and any(a.get('final_blow') for a in km_data['attackers'])
                    has_final_blow_char = (
                        'attackers' in km_data and
                        any(a.get('final_blow') and a.get('character_id') for a in km_data['attackers'])
                    )
                    needs_esi = (
                        any(k not in km_data for k in ['killmail_time', 'solar_system_id', 'victim', 'attackers']) or
                        not has_final_blow_char or
                        not has_all_attackers
                    )

        if needs_esi:
            if km_hash:
                reason = "missing fields"
                if not has_final_blow_char:
                    reason = "missing final blow character"
                elif not has_final_blow:
                    reason = "missing final blow"
                if not has_all_attackers:
                    reason = f"incomplete attackers ({len(km_data.get('attackers', []))}/{attacker_count})"
                logger.info(f"Killmail {km_id} needs ESI fetch ({reason}), attempting to fetch")
                esi_data = fetch_killmail_from_esi(km_id_val, km_hash)
                if esi_data:
                    logger.debug(f"Successfully fetched killmail {km_id} from ESI")
                    km_data.update(esi_data)
                    has_final_blow_char = (
                        'attackers' in km_data and
                        any(a.get('final_blow') and a.get('character_id') for a in km_data['attackers'])
                    )
                    if not has_final_blow_char:
                        logger.warning(f"Killmail {km_id} missing final blow character after ESI fetch")
                        if allow_incomplete:
                            return None
                        return False
                else:
                    logger.warning(f"Killmail {km_id} missing required fields and ESI fetch failed (ID: {km_id_val}, Hash: {km_hash})")
                    if allow_incomplete:
                        return None
                    return False
            else:
                logger.warning(f"Killmail {km_id} missing required fields and no hash available for ESI fetch")
                if allow_incomplete:
                    return None
                return False

    # Time check
    try:
        km_time = timezone.datetime.fromisoformat(km_data['killmail_time'].replace('Z', '+00:00'))
        if timezone.is_naive(km_time):
            km_time = timezone.make_aware(km_time)
    except (ValueError, TypeError) as e:
        logger.error(f"Killmail {km_id} has invalid time format: {km_data.get('killmail_time')} - {e}")
        if allow_incomplete:
            return None
        return False

    if km_time < campaign.start_date:
        logger.debug(f"Killmail {km_id} skipped for campaign {campaign}: before campaign start ({km_time} < {campaign.start_date})")
        return False
    if campaign.end_date and km_time > campaign.end_date:
        logger.debug(f"Killmail {km_id} skipped for campaign {campaign}: after campaign end")
        return False

    # Involvement check
    if campaign_meta and campaign.id in campaign_meta:
        friendly_ids = campaign_meta[campaign.id]['friendly_ids']
    else:
        friendly_ids = get_campaign_friendly_ids(campaign)

    friendly_involved = is_entity_involved(km_data, friendly_ids)

    if not friendly_involved:
        logger.debug(f"Killmail {km_id} skipped for campaign {campaign}: no friendly involvement. Attackers: {len(km_data.get('attackers', []))}")
        return False

    # Target check
    if campaign_meta and campaign.id in campaign_meta:
        target_ids = campaign_meta[campaign.id]['target_ids']
    else:
        target_ids = get_campaign_target_ids(campaign)

    has_targets = any(target_ids.values())
    target_involved = is_entity_involved(km_data, target_ids)

    if has_targets and not target_involved:
        logger.debug(f"Killmail {km_id} skipped for campaign {campaign}: target required but not involved")
        return False

    # Check if campaign is location restricted
    if campaign_meta and campaign.id in campaign_meta:
        has_locations = (
            campaign_meta[campaign.id]['system_ids'] or
            campaign_meta[campaign.id]['region_ids'] or
            campaign_meta[campaign.id]['constellation_ids']
        )
    else:
        has_locations = (
            campaign.systems.exists() or
            campaign.regions.exists() or
            campaign.constellations.exists()
        )

    if not has_locations:
        if has_targets:
            logger.info(f"Killmail {km_id} matched for campaign {campaign}: target involved (no locations)")
        else:
            # Global campaign with no specific targets -> match everything involving friendly
            logger.info(f"Killmail {km_id} matched for campaign {campaign}: global campaign (no targets/locations)")
        return True

    # Location check
    system_id = km_data.get('solar_system_id')
    if not system_id:
        logger.warning(f"Killmail {km_id} missing solar_system_id even after ESI fetch/DB lookup")
        if allow_incomplete:
            return None
        return False

    location_match = False
    system = None
    if context and system_id in context.get('resolved_systems', {}):
        system = context['resolved_systems'][system_id]
    else:
        try:
            system = EveSolarSystem.objects.get(id=system_id)
            if context:
                context.setdefault('resolved_systems', {})[system_id] = system
        except EveSolarSystem.DoesNotExist:
            system = None

    if campaign_meta and campaign.id in campaign_meta:
        if system_id in campaign_meta[campaign.id]['system_ids']:
            location_match = True
        elif system:
            system_region_id = _get_system_region_id(system)
            system_constellation_id = _get_system_constellation_id(system)
            if system_region_id in campaign_meta[campaign.id]['region_ids']:
                location_match = True
            elif system_constellation_id in campaign_meta[campaign.id]['constellation_ids']:
                location_match = True
    else:
        if campaign.systems.filter(id=system_id).exists():
            location_match = True
        elif system:
            system_region_id = _get_system_region_id(system)
            system_constellation_id = _get_system_constellation_id(system)
            if system_region_id and campaign.regions.filter(id=system_region_id).exists():
                location_match = True
            elif system_constellation_id and campaign.constellations.filter(id=system_constellation_id).exists():
                location_match = True

    if not location_match:
        logger.debug(f"Killmail {km_id} skipped for campaign {campaign}: location mismatch")
        return False

    if has_targets:
        logger.info(f"Killmail {km_id} matched for campaign {campaign}: target and location match")
    else:
        logger.info(f"Killmail {km_id} matched for campaign {campaign}: location match")
    return True

def get_campaign_friendly_ids(campaign):
    # Cache this maybe?
    ids = {'characters': set(), 'corporations': set(), 'alliances': set(), 'factions': set()}
    for member in campaign.members.all():
        if member.character:
            ids['characters'].add(member.character.character_id)
        if member.corporation:
            ids['corporations'].add(member.corporation.corporation_id)
        if member.alliance:
            ids['alliances'].add(member.alliance.alliance_id)
        if member.faction:
            ids['factions'].add(member.faction.faction_id)
    return ids

def get_campaign_target_ids(campaign):
    ids = {'characters': set(), 'corporations': set(), 'alliances': set(), 'factions': set()}
    for target in campaign.targets.all():
        if target.character:
            ids['characters'].add(target.character.character_id)
        if target.corporation:
            ids['corporations'].add(target.corporation.corporation_id)
        if target.alliance:
            ids['alliances'].add(target.alliance.alliance_id)
        if target.faction:
            ids['factions'].add(target.faction.faction_id)
    return ids

def is_entity_involved(km_data, entity_ids):
    faction_ids = entity_ids.get('factions', set())
    # Check attackers
    for attacker in km_data.get('attackers', []):
        if attacker.get('character_id') in entity_ids['characters']:
            return True
        if attacker.get('corporation_id') in entity_ids['corporations']:
            return True
        if attacker.get('alliance_id') in entity_ids['alliances']:
            return True
        if attacker.get('faction_id') in faction_ids:
            return True

    # Check victim
    victim = km_data.get('victim', {})
    if victim.get('character_id') in entity_ids['characters']:
        return True
    if victim.get('corporation_id') in entity_ids['corporations']:
        return True
    if victim.get('alliance_id') in entity_ids['alliances']:
        return True
    if victim.get('faction_id') in faction_ids:
        return True

    return False

def process_killmail(campaign, km_data, campaign_meta=None, context=None):
    _ensure_eve_models()
    km_id = km_data['killmail_id']
    try:
        km_time = timezone.datetime.fromisoformat(km_data['killmail_time'].replace('Z', '+00:00'))
        if timezone.is_naive(km_time):
            km_time = timezone.make_aware(km_time)
    except (KeyError, ValueError, TypeError):
        logger.error(f"Failed to parse killmail_time for killmail {km_id}")
        return

    def get_name(eid, name_hint=None):
        def cache_name(name):
            if context and eid:
                context.setdefault('resolved_names', {})[eid] = name
            return name

        if name_hint and name_hint != "Unknown":
            return cache_name(name_hint)
        if not eid:
            return ""
        if context and eid in context.get('resolved_names', {}):
            return context['resolved_names'][eid]

        data = _fetch_universe_names([eid])
        if data:
            return cache_name(data[0].get('name', "Unknown"))
        return "Unknown"

    # Is it a loss for our side?
    if campaign_meta and campaign.id in campaign_meta:
        friendly_ids = campaign_meta[campaign.id]['friendly_ids']
    else:
        friendly_ids = get_campaign_friendly_ids(campaign)

    victim = km_data.get('victim', {})
    is_loss = False
    if (victim.get('character_id') in friendly_ids['characters'] or
        victim.get('corporation_id') in friendly_ids['corporations'] or
        victim.get('alliance_id') in friendly_ids['alliances'] or
        victim.get('faction_id') in friendly_ids['factions']):
        is_loss = True

    # Resolve names
    victim_id = victim.get('character_id') or 0
    victim_corp_id = victim.get('corporation_id') or 0
    victim_alliance_id = victim.get('alliance_id')

    ship_type_id = victim.get('ship_type_id') or 0
    ship_type_name = "Unknown"
    ship_group_name = "Unknown"
    if ship_type_id:
        ship_type_name = get_name(ship_type_id, victim.get('ship_type_name'))
        try:
            # Also get ship group name for stats
            s_type = None
            s_type = _get_item_type(ship_type_id, context)

            if s_type:
                if ship_type_name in ("", "Unknown"):
                    ship_type_name = getattr(s_type, "name", ship_type_name)
                ship_group_name = _get_type_group_name(s_type)
        except Exception as e:
            logger.warning(f"Failed to get ship group for {ship_type_id}: {e}")

    victim_name = (
        get_name(victim_id, victim.get('character_name'))
        if (victim_id or victim.get('character_name'))
        else "Unknown"
    )
    victim_corp_name = (
        get_name(victim_corp_id, victim.get('corporation_name'))
        if (victim_corp_id or victim.get('corporation_name'))
        else "Unknown"
    )
    victim_alliance_name = (
        get_name(victim_alliance_id, victim.get('alliance_name'))
        if (victim_alliance_id or victim.get('alliance_name'))
        else ""
    )

    # Resolve Final Blow attacker
    final_blow_attacker = next((a for a in km_data.get('attackers', []) if a.get('final_blow')), {})
    if not final_blow_attacker:
        logger.warning(f"Killmail {km_id} has no attacker marked as final blow. Attackers count: {len(km_data.get('attackers', []))}")

    fb_char_id = final_blow_attacker.get('character_id') or 0
    fb_corp_id = final_blow_attacker.get('corporation_id') or 0
    fb_alliance_id = final_blow_attacker.get('alliance_id')

    fb_char_name = (
        get_name(fb_char_id, final_blow_attacker.get('character_name'))
        if (fb_char_id or final_blow_attacker.get('character_name'))
        else ""
    )
    fb_corp_name = (
        get_name(fb_corp_id, final_blow_attacker.get('corporation_name'))
        if (fb_corp_id or final_blow_attacker.get('corporation_name'))
        else "Unknown"
    )
    fb_alliance_name = (
        get_name(fb_alliance_id, final_blow_attacker.get('alliance_name'))
        if (fb_alliance_id or final_blow_attacker.get('alliance_name'))
        else ""
    )

    # Get system
    system_id = km_data['solar_system_id']
    system = None
    if context and system_id in context.get('resolved_systems', {}):
        system = context['resolved_systems'][system_id]
    else:
        try:
            system = EveSolarSystem.objects.get(id=system_id)
            if context: context.setdefault('resolved_systems', {})[system_id] = system
        except EveSolarSystem.DoesNotExist:
            system = None

    with transaction.atomic():
        ckm, created = CampaignKillmail.objects.update_or_create(
            campaign=campaign,
            killmail_id=km_id,
            defaults={
                'killmail_time': km_time,
                'solar_system': system,
                'ship_type_id': ship_type_id,
                'ship_type_name': ship_type_name,
                'ship_group_name': ship_group_name,
                'victim_id': victim_id,
                'victim_name': victim_name,
                'victim_corp_id': victim_corp_id,
                'victim_corp_name': victim_corp_name,
                'victim_alliance_id': victim_alliance_id,
                'victim_alliance_name': victim_alliance_name,
                'final_blow_char_id': fb_char_id,
                'final_blow_char_name': fb_char_name,
                'final_blow_corp_id': fb_corp_id,
                'final_blow_corp_name': fb_corp_name,
                'final_blow_alliance_id': fb_alliance_id,
                'final_blow_alliance_name': fb_alliance_name,
                'total_value': km_data.get('zkb', {}).get('totalValue', 0),
                'is_loss': is_loss,
            }
        )

        # Update attackers
        friendly_attackers = []
        for attacker in km_data.get('attackers', []):
            char_id = attacker.get('character_id')
            corp_id = attacker.get('corporation_id')
            alliance_id = attacker.get('alliance_id')

            is_friendly = (
                (char_id and char_id in friendly_ids['characters']) or
                (corp_id and corp_id in friendly_ids['corporations']) or
                (alliance_id and alliance_id in friendly_ids['alliances']) or
                (attacker.get('faction_id') in friendly_ids['factions'])
            )

            if is_friendly and char_id:
                char = None
                if context and char_id in context.get('resolved_characters', {}):
                    char = context['resolved_characters'][char_id]
                else:
                    try:
                        char = EveCharacter.objects.get(character_id=char_id)
                    except EveCharacter.DoesNotExist:
                        try:
                            # create_character fetches from ESI and creates the object
                            char = EveCharacter.objects.create_character(char_id)
                        except Exception as e:
                            logger.warning(f"Failed to create EveCharacter for {char_id}: {e}")
                            char = None
                    if context: context.setdefault('resolved_characters', {})[char_id] = char

                if char:
                    friendly_attackers.append(char)

        if friendly_attackers:
            ckm.attackers.set(friendly_attackers)
