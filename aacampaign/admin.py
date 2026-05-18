"""Admin models"""

from django.contrib import admin
from .models import Campaign, CampaignMember, CampaignTarget, CampaignKillmail

class CampaignMemberInline(admin.TabularInline):
    model = CampaignMember
    extra = 1
    raw_id_fields = ('character', 'corporation', 'alliance', 'faction')

class CampaignTargetInline(admin.TabularInline):
    model = CampaignTarget
    extra = 1
    raw_id_fields = ('character', 'corporation', 'alliance', 'faction')

@admin.register(Campaign)
class CampaignAdmin(admin.ModelAdmin):
    list_display = ('name', 'start_date', 'end_date', 'is_active')
    inlines = [CampaignMemberInline, CampaignTargetInline]
    filter_horizontal = ('systems', 'regions', 'constellations')

@admin.register(CampaignKillmail)
class CampaignKillmailAdmin(admin.ModelAdmin):
    list_display = ('killmail_id', 'campaign', 'killmail_time', 'victim_name', 'total_value', 'is_loss')
    list_filter = ('campaign', 'is_loss', 'solar_system')
    search_fields = ('victim_name', 'victim_corp_name', 'victim_alliance_name')
    raw_id_fields = ('solar_system', 'attackers')
