from django.core.management.base import BaseCommand
from django_celery_beat.models import PeriodicTask, CrontabSchedule
import json

class Command(BaseCommand):
    help = 'Setup periodic tasks for AA Campaign'

    def handle(self, *args, **options):
        # Setup hourly pull task
        self.stdout.write("Setting up periodic tasks for AA Campaign...")
        schedule, _ = CrontabSchedule.objects.get_or_create(
            minute='0',
            hour='*',
            day_of_week='*',
            day_of_month='*',
            month_of_year='*',
        )

        task_name = 'aacampaign.tasks.pull_zkillboard_data'
        PeriodicTask.objects.update_or_create(
            name='AA Campaign: Pull ZKillboard Data',
            defaults={
                'task': task_name,
                'crontab': schedule,
                'enabled': True,
            }
        )

        cleanup_schedule, _ = CrontabSchedule.objects.get_or_create(
            minute='30',
            hour='3',
            day_of_week='*',
            day_of_month='*',
            month_of_year='*',
        )

        PeriodicTask.objects.update_or_create(
            name='AA Campaign: Cleanup Campaign Killmails',
            defaults={
                'task': 'aacampaign.tasks.cleanup_campaign_killmails',
                'crontab': cleanup_schedule,
                'enabled': True,
            }
        )

        self.stdout.write(self.style.SUCCESS('Successfully setup periodic tasks for AA Campaign.'))
        self.stdout.write("Please ensure your Celery worker and beat services are running.")
