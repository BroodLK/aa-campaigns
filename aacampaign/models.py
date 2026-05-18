"""
App Models
Create your models in here
"""

# Django
from django.db import models
from allianceauth.eveonline.models import (
    EveAllianceInfo,
    EveCorporationInfo,
    EveCharacter,
    EveFactionInfo,
)

# AA Campaign
from .eve_sde import (
    CONSTELLATION_MODEL,
    REGION_MODEL,
    SOLAR_SYSTEM_MODEL,
    get_system_region,
)

class Campaign(models.Model):
    """A Z-Kill campaign"""
    name = models.CharField(max_length=255)
    description = models.TextField(blank=True)
    start_date = models.DateTimeField()
    end_date = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    last_run = models.DateTimeField(null=True, blank=True, default=None)

    systems = models.ManyToManyField(
        SOLAR_SYSTEM_MODEL, blank=True, related_name="campaigns"
    )
    regions = models.ManyToManyField(
        REGION_MODEL, blank=True, related_name="campaigns"
    )
    constellations = models.ManyToManyField(
        CONSTELLATION_MODEL, blank=True, related_name="campaigns"
    )

    class Meta:
        default_permissions = ()
        permissions = (
            ("basic_access", "Can access this app"),
            ("manage_campaign", "Can manage campaigns"),
        )

    def __str__(self):
        return self.name

class CampaignMember(models.Model):
    """A friendly entity in a campaign"""
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='members')
    character = models.ForeignKey(EveCharacter, on_delete=models.CASCADE, null=True, blank=True)
    corporation = models.ForeignKey(EveCorporationInfo, on_delete=models.CASCADE, null=True, blank=True)
    alliance = models.ForeignKey(EveAllianceInfo, on_delete=models.CASCADE, null=True, blank=True)
    faction = models.ForeignKey(EveFactionInfo, on_delete=models.CASCADE, null=True, blank=True)

    def __str__(self):
        return str(self.character or self.corporation or self.alliance or self.faction)

class CampaignTarget(models.Model):
    """A specific hostile entity in a campaign"""
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='targets')
    character = models.ForeignKey(EveCharacter, on_delete=models.CASCADE, null=True, blank=True)
    corporation = models.ForeignKey(EveCorporationInfo, on_delete=models.CASCADE, null=True, blank=True)
    alliance = models.ForeignKey(EveAllianceInfo, on_delete=models.CASCADE, null=True, blank=True)
    faction = models.ForeignKey(EveFactionInfo, on_delete=models.CASCADE, null=True, blank=True)

    def __str__(self):
        return str(self.character or self.corporation or self.alliance or self.faction)

class CampaignKillmail(models.Model):
    """A killmail associated with a campaign"""
    campaign = models.ForeignKey(Campaign, on_delete=models.CASCADE, related_name='killmails')
    killmail_id = models.BigIntegerField()
    killmail_time = models.DateTimeField()
    solar_system = models.ForeignKey(
        SOLAR_SYSTEM_MODEL, on_delete=models.SET_NULL, null=True
    )

    ship_type_id = models.IntegerField(default=0)
    ship_type_name = models.CharField(max_length=255, default="")
    ship_group_name = models.CharField(max_length=255, default="")

    victim_id = models.BigIntegerField()
    victim_name = models.CharField(max_length=255)
    victim_corp_id = models.BigIntegerField()
    victim_corp_name = models.CharField(max_length=255)
    victim_alliance_id = models.BigIntegerField(null=True, blank=True)
    victim_alliance_name = models.CharField(max_length=255, blank=True)

    # Final blow attacker (can be anyone, including hostile)
    final_blow_char_id = models.BigIntegerField(default=0)
    final_blow_char_name = models.CharField(max_length=255, default="")
    final_blow_corp_id = models.BigIntegerField(default=0)
    final_blow_corp_name = models.CharField(max_length=255, default="")
    final_blow_alliance_id = models.BigIntegerField(null=True, blank=True)
    final_blow_alliance_name = models.CharField(max_length=255, blank=True, default="")

    # We store the friendly characters who participated
    attackers = models.ManyToManyField(EveCharacter, related_name='campaign_killmails')

    total_value = models.DecimalField(max_digits=20, decimal_places=2)
    is_loss = models.BooleanField(default=False)

    class Meta:
        unique_together = ('campaign', 'killmail_id')

    def __str__(self):
        return f"{self.campaign.name} - {self.killmail_id}"

    @property
    def solar_system_region(self):
        return get_system_region(self.solar_system)

    @property
    def solar_system_region_name(self):
        region = self.solar_system_region
        return getattr(region, "name", "") if region else ""
