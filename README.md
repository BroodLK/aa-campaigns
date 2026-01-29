![PyPI - Version](https://img.shields.io/pypi/v/aa-campaign?style=for-the-badge)
![PyPI - Downloads](https://img.shields.io/pypi/dm/aa-campaign?style=for-the-badge)
![PyPI - Format](https://img.shields.io/pypi/format/aa-campaign?style=for-the-badge)
![python versions](https://img.shields.io/pypi/pyversions/aa-campaign?style=for-the-badge)
![django versions](https://img.shields.io/badge/django-4.2%2B-blue?style=for-the-badge)
![license](https://img.shields.io/badge/license-GPLv3-green?style=for-the-badge)


> [!IMPORTANT]
> AA Campaign requires a working installation of **Alliance Auth** and **django-eveuniverse**. Ensure these are set up before proceeding with the installation.

# AA Campaign
AA Campaign is a plugin for **Alliance Auth** that allows you to create and track ZKill campaigns. Whether you're monitoring a specific system, a whole region, or targeting a rival alliance, AA Campaign pulls data directly from ZKillboard to provide real-time intelligence and performance tracking.

## Screenshots
<img src="https://i.imgur.com/UZWiWMh.png" alt="AA Campaign dashboard overview" width="900" style="max-width: 100%; height: auto;">
<img src="https://i.imgur.com/sY9H53O.png" alt="AA Campaign killmails view" width="900" style="max-width: 100%; height: auto;">

## Index

- [AA Campaign](#aa-campaign)
  - [Core Requirements](#core-requirements)
  - [Install Instructions](#install-instructions)
- [Features](#features)
  - [Campaign Dashboard](#campaign-dashboard)
  - [Leaderboards](#leaderboards)
  - [Recent Killmails](#recent-killmails)
  - [Ship Class Statistics](#ship-class-statistics)
- [Permissions](#permissions)

## Core Requirements
### The following AllianceAuth plugins are **_required_**:

```md
allianceauth >= 4.12
django-eveuniverse
```

## Install Instructions
After making sure to add the above prerequisite applications.
```bash
source /home/allianceserver/venv/auth/bin/activate && cd /home/allianceserver/myauth/
```
```bash
pip install aa-campaign==1.0.3
```
```bash
vi myauth/settings/local.py
```
Add `aacampaign` to your `INSTALLED_APPS`. Ensure that `eveuniverse` is also present in `INSTALLED_APPS`.
```bash
python manage.py migrate && python manage.py collectstatic --noinput
python manage.py aa_campaign_setup
```
restart the things
exit your venv
```bash
sudo supervisorctl restart myauth:
```

> [!TIP]
> You can manually trigger a data pull using:
> ```bash
> python manage.py aa_campaign_pull --days 30
> ````
>
> adding `--verbose` to the end will display the raw data pulled from ZKillboard.
>
> You can repair killmails using:
> ```bash
> python manage.py aa_campaign_pull --repair
> ```

> [!WARNING]
> Running a long pull, or setting a campaign set far into the past can take an extremely long time.
> This app is designed with politeness towards API first and foremost. The more data you pull, the longer it will take.

# Features

## Campaign Dashboard
### The AA Campaign dashboard provides a unified view of all active operations.
Track your progress with a suite of analytical tools and live data feeds. Selecting a campaign displays a detailed breakdown of performance.

- **Campaign Stats**
  - Instant overview of total kills, losses, ISK value, and overall efficiency.
- **Location Tracking**
  - Campaigns can be locked to specific Solar Systems, Constellations, or Regions, or set to Global for entity-wide tracking.
- **Member vs Target Tracking**
  - Define exactly which friendly characters, corporations, or alliances are participating and which hostile entities are the targets.

## Leaderboards
### Compete for the top spot with integrated character leaderboards.
- Tracks individual performance within each campaign.
- Ranks pilots by kill count and total ISK value.
- Highlights top performers with rank icons for the top 5.

## Recent Killmails
### A detailed feed of all activity associated with your campaign.
- Color-coded indicators for kills (green) and losses (red).
- Direct links to ZKillboard for detailed analysis.
- Summarized victim and final blow information for quick review.

## Ship Class Statistics
### Analyze the meta of your campaign.
- Breakdown of kills and losses by ship class.
- Helps identify what ships are being used effectively and where losses are occurring.
- Interactive data tables for easy filtering and sorting.

# Permissions

| Permission            | Description             |
|-----------------------|-------------------------|
| **basic_access**      | Can access this app     |
| **manage_campaign**   | Can manage campaigns    |
