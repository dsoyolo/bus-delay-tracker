# bus-delay-tracker

Monitors North Bay Ontario Transit routes via the Google Maps Directions API and sends SMS alerts to your phone via AWS SNS when delays are detected.

## How it works

1. You configure routes in `config.yaml` (origin, destination, departure time, days).
2. On first run, you record a **baseline** — the normal journey time on a delay-free day.
3. The tracker polls Google Maps every few minutes and compares the current estimate against the baseline.
4. If the current estimate exceeds the baseline by your threshold (default: 5 min), an SMS is sent to your phone via AWS SNS.

## Setup

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Store your Google Maps API key in AWS Secrets Manager

The tracker reads credentials from a single Secrets Manager secret named `bus-delay-tracker/credentials`. Create it with:

```bash
aws secretsmanager create-secret \
  --name bus-delay-tracker/credentials \
  --region ca-central-1 \
  --secret-string '{"google_maps_api_key": "YOUR_KEY_HERE"}'
```

Enable the **Directions API** for your key at [console.cloud.google.com](https://console.cloud.google.com).

### 3. Configure AWS credentials (no keys on disk)

Use one of these approaches — in preference order:

| Scenario | How |
|----------|-----|
| Running on EC2 / Lambda / ECS | Attach an IAM role — nothing else needed |
| Local development | `aws configure` — writes to `~/.aws/credentials`, outside this repo |
| Last resort | Set `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` as env vars |

The IAM identity (user or role) needs two permissions:
- `secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:*:*:secret:bus-delay-tracker/*`
- `sns:Publish`

### 4. Configure your routes

Edit `config.yaml`:

```yaml
routes:
  - name: "Morning Commute"
    origin: "123 Main St, North Bay, ON"
    destination: "Downtown North Bay Transit Terminal"
    check_time: "07:30"      # 24h local time
    days: ["mon","tue","wed","thu","fri"]
    delay_threshold_minutes: 5

alerts:
  phone_number: "+16131234567"   # Your phone in E.164 format
  alert_lead_minutes: 15         # Alert X min before departure
  cooldown_hours: 12             # Max one alert per route per 12h
```

### 5. Learn the baseline

Run this once on a normal (non-delayed) day near your usual commute time:

```bash
python tracker.py --learn-baseline
```

This saves journey times to `baseline.json`.

### 6. Run

**One-shot check:**
```bash
python tracker.py --check-now
```

**Continuous daemon** (polls every N minutes as configured):
```bash
python tracker.py --daemon
```

**Run as a cron job** (checks every 5 minutes on weekday mornings):
```cron
*/5 7-9 * * 1-5 cd /path/to/bus-delay-tracker && .venv/bin/python tracker.py --check-now
```

## AWS SNS setup

1. In your AWS account, verify your phone number under **SNS > Text messaging (SMS) > Sandbox** if still in sandbox mode
2. For production volume, request to move out of the SMS sandbox

## Files

| File | Purpose |
|------|---------|
| `tracker.py` | Main script — route checking and scheduling |
| `notifier.py` | AWS SNS SMS sender |
| `secrets.py` | AWS Secrets Manager client with in-process cache |
| `config.yaml` | Routes and alert settings |
| `baseline.json` | Auto-generated — normal journey times |
| `.alert_state.json` | Auto-generated — cooldown tracking |
