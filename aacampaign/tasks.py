"""App Tasks"""

# Standard Library
import logging
import requests
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from django.utils import timezone
from django.conf import settings
from django.core.cache import cache
from celery import shared_task
from .models import Campaign, CampaignKillmail, CampaignMember, CampaignTarget
from .esi import esi, call_result
from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo, EveAllianceInfo
from eveuniverse.models import EveSolarSystem, EveEntity, EveType, EveConstellation, EveRegion
from django.db import transaction
from django.db.models import Q

logger = logging.getLogger(__name__)

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
        data, _ = call_result(
            lambda: esi.client.Universe.PostUniverseNames,
            body=ids
        )
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
        elif has_filters:
            # No friendlies configured; fall back to targets and locations.
            for target in campaign.targets.all():
                if target.character: add_raw_entity('characterID', target.character.character_id)
                if target.corporation: add_raw_entity('corporationID', target.corporation.corporation_id)
                if target.alliance: add_raw_entity('allianceID', target.alliance.alliance_id)

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
    system_info = {s.id: (s.eve_constellation_id, s.eve_constellation.eve_region_id)
                   for s in EveSolarSystem.objects.filter(id__in=system_ids).select_related('eve_constellation')}
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
    const_info = {c.id: c.eve_region_id for c in EveConstellation.objects.filter(id__in=const_ids)}
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
            new_systems = EveSolarSystem.objects.filter(id__in=missing_system_ids).select_related('eve_constellation__eve_region')
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
            while page <= 20: # Should be plenty
                kms = fetch_from_zkill(entity_type, entity_id, past_seconds=seconds_to_pull, page=page)
                if not kms:
                    break

                logger.info(f"Fetched page {page} ({len(kms)} kills) for {entity_type} {entity_id} using pastSeconds")
                new_on_page = process_page_of_kms(kms)
                logger.info(f"Processed {new_on_page} unique killmails from page {page}")

                if len(kms) < 200: # Last page
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

                while page <= max_pages_per_month:
                    kms = fetch_from_zkill(entity_type, entity_id, page=page, year=curr_year, month=curr_month)
                    if kms is None:
                        logger.error(f"Failed to fetch page {page} for {curr_year}-{curr_month:02d}. Skipping month.")
                        break

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

    try:
        response = _zkill_get(url)
        data = response.json()
        if not isinstance(data, list):
            logger.error(
                f"Unexpected response from zKillboard for {entity_type} {entity_id}: "
                f"expected list, got {type(data)}. Content: {data}"
            )
            return None
        data = [km for km in data if isinstance(km, dict)]
        if not data:
            logger.debug(f"All results were null from zKillboard for {entity_type} {entity_id}")
            return None
        logger.debug(f"Fetched {len(data)} results from zKillboard for {entity_type} {entity_id}")
        return data
    except Exception as e:
        logger.error(f"Error fetching from zkillboard for {entity_type} {entity_id}: {e}")
        return None

def fetch_killmail_from_esi(killmail_id, killmail_hash):
    try:
        logger.debug(f"Fetching killmail {killmail_id} from ESI")
        data, _ = call_result(
            lambda: esi.client.Killmails.GetKillmailsKillmailIdKillmailHash,
            killmail_id=killmail_id,
            killmail_hash=killmail_hash
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

def should_include_killmail(campaign, km_data, campaign_meta=None, context=None):
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
                        return False
                else:
                    logger.warning(f"Killmail {km_id} missing required fields and ESI fetch failed (ID: {km_id_val}, Hash: {km_hash})")
                    return False
            else:
                logger.warning(f"Killmail {km_id} missing required fields and no hash available for ESI fetch")
                return False

    # Time check
    try:
        km_time = timezone.datetime.fromisoformat(km_data['killmail_time'].replace('Z', '+00:00'))
        if timezone.is_naive(km_time):
            km_time = timezone.make_aware(km_time)
    except (ValueError, TypeError) as e:
        logger.error(f"Killmail {km_id} has invalid time format: {km_data.get('killmail_time')} - {e}")
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

    if target_involved:
        logger.info(f"Killmail {km_id} matched for campaign {campaign}: target involved")
        return True

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
        if not has_targets:
            # Global campaign with no specific targets -> match everything involving friendly
            logger.info(f"Killmail {km_id} matched for campaign {campaign}: global campaign (no targets/locations)")
            return True
        else:
            # Global campaign with targets -> must match a target (already checked above)
            logger.debug(f"Killmail {km_id} skipped for campaign {campaign}: global campaign, but no target match")
            return False

    # Location check
    system_id = km_data.get('solar_system_id')
    if not system_id:
        logger.warning(f"Killmail {km_id} missing solar_system_id even after ESI fetch/DB lookup")
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
            if system.eve_constellation.eve_region_id in campaign_meta[campaign.id]['region_ids']:
                location_match = True
            elif system.eve_constellation_id in campaign_meta[campaign.id]['constellation_ids']:
                location_match = True
    else:
        if campaign.systems.filter(id=system_id).exists():
            location_match = True
        elif system:
            if campaign.regions.filter(id=system.eve_constellation.eve_region_id).exists():
                location_match = True
            elif campaign.constellations.filter(id=system.eve_constellation_id).exists():
                location_match = True

    if location_match:
        logger.info(f"Killmail {km_id} matched for campaign {campaign}: location match")
        return True

    return False

def get_campaign_friendly_ids(campaign):
    # Cache this maybe?
    ids = {'characters': set(), 'corporations': set(), 'alliances': set()}
    for member in campaign.members.all():
        if member.character:
            ids['characters'].add(member.character.character_id)
        if member.corporation:
            ids['corporations'].add(member.corporation.corporation_id)
        if member.alliance:
            ids['alliances'].add(member.alliance.alliance_id)
    return ids

def get_campaign_target_ids(campaign):
    ids = {'characters': set(), 'corporations': set(), 'alliances': set()}
    for target in campaign.targets.all():
        if target.character:
            ids['characters'].add(target.character.character_id)
        if target.corporation:
            ids['corporations'].add(target.corporation.corporation_id)
        if target.alliance:
            ids['alliances'].add(target.alliance.alliance_id)
    return ids

def is_entity_involved(km_data, entity_ids):
    # Check attackers
    for attacker in km_data.get('attackers', []):
        if attacker.get('character_id') in entity_ids['characters']:
            return True
        if attacker.get('corporation_id') in entity_ids['corporations']:
            return True
        if attacker.get('alliance_id') in entity_ids['alliances']:
            return True

    # Check victim
    victim = km_data.get('victim', {})
    if victim.get('character_id') in entity_ids['characters']:
        return True
    if victim.get('corporation_id') in entity_ids['corporations']:
        return True
    if victim.get('alliance_id') in entity_ids['alliances']:
        return True

    return False

def process_killmail(campaign, km_data, campaign_meta=None, context=None):
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
        victim.get('alliance_id') in friendly_ids['alliances']):
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
            if context and ship_type_id in context.get('resolved_types', {}):
                s_type = context['resolved_types'][ship_type_id]
            else:
                s_type, _ = EveType.objects.get_or_create_esi(id=ship_type_id)
                if context: context.setdefault('resolved_types', {})[ship_type_id] = s_type

            if s_type:
                if ship_type_name in ("", "Unknown"):
                    ship_type_name = getattr(s_type, "name", ship_type_name)
                if s_type.eve_group:
                    ship_group_name = s_type.eve_group.name
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
                (alliance_id and alliance_id in friendly_ids['alliances'])
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
