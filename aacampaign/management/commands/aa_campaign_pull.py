# Standard Library
import logging

# Django
from django.core.management.base import BaseCommand, CommandError

# AA Campaign
from aacampaign.models import CampaignKillmail
from aacampaign.tasks import (
    fix_killmail_stats,
    get_killmail_repair_filter,
    get_killmail_stats_fix_filter,
    pull_zkillboard_data,
    repair_killmail_by_id,
)


class _AacampaignPullLogFilter(logging.Filter):
    def filter(self, record):
        if record.levelno >= logging.ERROR:
            return True
        message = record.getMessage()
        return (
            message.startswith("ESI rate limit remaining")
            or message.startswith("ESI rate limit hit")
            or message.startswith("ESI rate limit remaining low")
        )


def _configure_logging(verbose):
    if verbose:
        logging.getLogger("aacampaign").setLevel(logging.DEBUG)
        return

    logging.getLogger("aacampaign").setLevel(logging.INFO)
    for logger_name in (
        "esi",
        "esi.aiopenapi3",
        "esi.openapi_clients",
        "httpx",
        "urllib3",
    ):
        logging.getLogger(logger_name).setLevel(logging.ERROR)

    log_filter = _AacampaignPullLogFilter()
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _AacampaignPullLogFilter) for f in handler.filters):
            handler.addFilter(log_filter)
    for handler in logging.getLogger("aacampaign").handlers:
        if not any(isinstance(f, _AacampaignPullLogFilter) for f in handler.filters):
            handler.addFilter(log_filter)


class Command(BaseCommand):
    help = "Manually trigger pulling ZKillboard data or repairing existing data"

    def add_arguments(self, parser):
        parser.add_argument(
            "--seconds",
            type=int,
            help="Pull data for the last X seconds",
        )
        parser.add_argument(
            "--days",
            type=int,
            help="Pull data for the last X days",
        )
        parser.add_argument(
            "--repair",
            action="store_true",
            help="Attempt to repair existing killmails with missing information",
        )
        parser.add_argument(
            "--fix-stats",
            action="store_true",
            help="Backfill ship type and ship group names from SDE only",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Show detailed debug information during the pull",
        )

    def handle(self, *args, **options):
        verbose = options.get("verbose")
        _configure_logging(verbose)

        if options.get("repair") and options.get("fix_stats"):
            raise CommandError("--repair and --fix-stats cannot be used together")

        if options.get("fix_stats"):
            killmails_to_fix = CampaignKillmail.objects.filter(get_killmail_stats_fix_filter()).count()
            if killmails_to_fix == 0:
                self.stdout.write(self.style.SUCCESS("No killmail stats found in need of fixing"))
                return

            self.stdout.write(f"Fixing stats for {killmails_to_fix} killmails from SDE...")
            result = fix_killmail_stats()
            self.stdout.write(self.style.SUCCESS(f"Finished: {result}"))
            return

        if options.get("repair"):
            kms_to_repair = list(
                CampaignKillmail.objects.filter(get_killmail_repair_filter())
                .values_list("killmail_id", flat=True)
                .distinct()
            )
            total = len(kms_to_repair)
            if total == 0:
                self.stdout.write(self.style.SUCCESS("No killmails found in need of repair"))
                return

            self.stdout.write(f"Repairing {total} killmails with missing information...")
            repaired_count = 0
            for i, km_id in enumerate(kms_to_repair, 1):
                if repair_killmail_by_id(km_id):
                    repaired_count += 1

                # Progress indicator
                self.stdout.write(f"Progress: [{i}/{total}]", ending="\r")
                self.stdout.flush()

            self.stdout.write("")  # newline
            self.stdout.write(self.style.SUCCESS(f"Finished: Repaired {repaired_count} killmails"))
            return

        seconds = options.get("seconds")
        days = options.get("days")
        if days:
            seconds = days * 86400

        if days:
            self.stdout.write(f"Triggering ZKillboard data pull for the last {days} days...")
        elif seconds:
            self.stdout.write(f"Triggering ZKillboard data pull for the last {seconds} seconds...")
        else:
            self.stdout.write("Triggering ZKillboard data pull (defaulting to today's data)...")

        result = pull_zkillboard_data(past_seconds=seconds)
        self.stdout.write(self.style.SUCCESS(f"Finished: {result}"))
