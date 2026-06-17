"""App Views"""

# Django
from django.contrib.auth.decorators import login_required, permission_required
from django.core.handlers.wsgi import WSGIRequest
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render

from .eve_sde import get_item_type_group_names, get_solar_system_select_related_fields
from .models import Campaign


def build_ship_stats(campaign):
    ship_stats_raw = list(
        campaign.killmails.values("ship_group_name", "ship_type_id", "is_loss").annotate(count=Count("id"))
    )
    missing_group_type_ids = {
        entry["ship_type_id"]
        for entry in ship_stats_raw
        if entry["ship_type_id"] and entry["ship_group_name"] in ("", "Unknown", None)
    }
    resolved_group_names = get_item_type_group_names(missing_group_type_ids)

    ship_stats = {}
    for entry in ship_stats_raw:
        group = entry["ship_group_name"]
        if not group or group == "Unknown":
            group = resolved_group_names.get(entry["ship_type_id"], "Unknown")
        if group not in ship_stats:
            ship_stats[group] = {"killed": 0, "lost": 0}
        if entry["is_loss"]:
            ship_stats[group]["lost"] += entry["count"]
        else:
            ship_stats[group]["killed"] += entry["count"]

    return dict(sorted(ship_stats.items()))


@login_required
@permission_required("aacampaign.basic_access")
def index(request: WSGIRequest) -> HttpResponse:
    """List of active campaigns"""
    campaigns = Campaign.objects.filter(is_active=True).order_by("-start_date")
    context = {"campaigns": campaigns}
    return render(request, "aacampaign/index.html", context)


@login_required
@permission_required("aacampaign.basic_access")
def campaign_details(request: WSGIRequest, campaign_id: int) -> HttpResponse:
    """Leaderboard and stats for a campaign"""
    campaign = get_object_or_404(Campaign, id=campaign_id)

    # Overall stats
    stats = campaign.killmails.aggregate(
        total_kills=Count("id", filter=Q(is_loss=False)),
        total_kill_value=Sum("total_value", filter=Q(is_loss=False)),
        total_losses=Count("id", filter=Q(is_loss=True)),
        total_loss_value=Sum("total_value", filter=Q(is_loss=True)),
    )

    # Calculate efficiency
    if stats["total_kill_value"] and stats["total_loss_value"]:
        efficiency = (stats["total_kill_value"] / (stats["total_kill_value"] + stats["total_loss_value"])) * 100
    elif stats["total_kill_value"]:
        efficiency = 100
    else:
        efficiency = 0

    # Top kills for the bar
    top_kills = campaign.killmails.filter(is_loss=False).order_by("-total_value")[:10]

    # Ship Class stats
    ship_stats = build_ship_stats(campaign)

    # Recent killmails for the new tab
    recent_killmails = (
        campaign.killmails.select_related(*get_solar_system_select_related_fields())
        .prefetch_related("attackers")
        .order_by("-killmail_time")[:1000]
    )

    context = {
        "campaign": campaign,
        "stats": stats,
        "efficiency": efficiency,
        "top_kills": top_kills,
        "ship_stats": ship_stats,
        "recent_killmails": recent_killmails,
    }
    return render(request, "aacampaign/campaign_details.html", context)


@login_required
@permission_required("aacampaign.basic_access")
def leaderboard_data(request: WSGIRequest, campaign_id: int) -> JsonResponse:
    """JSON data for the leaderboard DataTable"""
    campaign = get_object_or_404(Campaign, id=campaign_id)

    # DataTables parameters
    draw = int(request.GET.get("draw", 1))
    start = int(request.GET.get("start", 0))
    length = int(request.GET.get("length", 10))
    search_value = request.GET.get("search[value]", "").lower()

    # Fetch all friendly involvements on successful kills for this campaign
    # We fetch enough fields to aggregate by User or individual Character
    involvements = campaign.killmails.filter(is_loss=False, attackers__isnull=False).values(
        "id",
        "total_value",
        "attackers__id",
        "attackers__character_name",
        "attackers__character_ownership__user_id",
        "attackers__character_ownership__user__profile__main_character__character_name",
    )

    # Grouping logic in Python to avoid double-counting kills/values
    # when multiple alts of the same user are on the same killmail.
    # groups key: ('U', user_id) or ('C', character_id)
    groups = {}

    for row in involvements:
        uid = row["attackers__character_ownership__user_id"]
        cid = row["attackers__id"]
        kid = row["id"]
        val = float(row["total_value"])

        if uid:
            key = ("U", uid)
            display_name = (
                row["attackers__character_ownership__user__profile__main_character__character_name"]
                or row["attackers__character_name"]
            )
        else:
            key = ("C", cid)
            display_name = row["attackers__character_name"]

        if key not in groups:
            groups[key] = {
                "character_name": display_name,
                "kills_set": set(),
                "kill_value": 0.0,
            }

        if kid not in groups[key]["kills_set"]:
            groups[key]["kills_set"].add(kid)
            groups[key]["kill_value"] += val

    # Convert aggregated data to list for DataTables processing
    # We include the key to help with rank calculation later
    data_list = []
    for key, stats in groups.items():
        stats["group_key"] = key
        stats["kills"] = len(stats.pop("kills_set"))
        data_list.append(stats)

    # Filtering (search)
    if search_value:
        data_list = [d for d in data_list if search_value in d["character_name"].lower()]

    records_filtered = len(data_list)

    # Sorting
    order_column_index = request.GET.get("order[0][column]")
    order_dir = request.GET.get("order[0][dir]", "desc")

    sort_columns = {
        "0": "character_name",
        "1": "kills",
        "2": "kill_value",
    }
    sort_field = sort_columns.get(order_column_index, "kill_value")
    data_list.sort(key=lambda x: x[sort_field], reverse=(order_dir == "desc"))

    # Paging
    paged_data = data_list[start : start + length]

    # Add rank metadata to paged data
    # Icons 1-5 always on the first 5 rows of the table (rank 1-5 overall in current sort)
    for i, entry in enumerate(paged_data):
        entry.pop("group_key", None)
        global_index = start + i
        if global_index < 5:
            entry["rank"] = global_index + 1
        else:
            entry["rank"] = None

    return JsonResponse(
        {
            "draw": draw,
            "recordsTotal": len(groups),
            "recordsFiltered": records_filtered,
            "data": paged_data,
        }
    )
