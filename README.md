# Project Saving Private Adam

Parental control system for Adam's Linux computer. Runs entirely on his
machine, managed remotely over SSH from your machine.

---

## Your scripts (run from your machine)

```bash
./deploy.sh              # push config + lists, restart daemon (~5 seconds)
./grant.sh free 60       # give 60 min of free time right now
./grant.sh free 30 +     # extend current grant by 30 min
./grant.sh work 45       # force work mode for 45 min
./grant.sh clear         # cancel grant, back to normal schedule
./grant.sh status        # show current grant
./status.sh              # full status dashboard
./status.sh logs         # tail live daemon logs
./status.sh proxy        # tail live proxy logs
```

---

## Day-to-day workflow

**Change a rule:** Edit [config/schedule.yaml](config/schedule.yaml) or drop
a `.txt` file into [config/lists/](config/lists/), then run `./deploy.sh`.

**Give extra time:** `./grant.sh free 60`

**Check what's happening:** `./status.sh` — or have Adam open
`http://localhost:8765` on his browser.

**Add a school site to the whitelist:** Edit
[config/lists/work/allowed.txt](config/lists/work/allowed.txt), run
`./deploy.sh`.

---

## How it works

A Python daemon runs as root via systemd on Adam's machine. Every 53 seconds:

1. Checks `computer_hours` — outside those hours the screen locks, full stop
2. Checks for an active grant (your manual override)
3. In **budget mode**: determines if free time is counting based on activity
4. Writes rules to a JSON file picked up by the local mitmproxy
5. Enforces `/etc/hosts` blocks, kills blocked apps, tracks time

**Two blocking tiers:**

| Tier | When active |
|---|---|
| `always/` | 24/7, every mode — adult sites, Reddit |
| `work/` | Whitelist-only during work mode. Not on the list = blocked. |

**Budget mode:** Adam gets a daily pool of free minutes. The timer only runs
when he's doing non-whitelisted activity (free sites, free apps, or VPN).
Doing homework on whitelisted school sites doesn't cost budget.

**Proxy:** mitmproxy intercepts all HTTP/HTTPS traffic so URL-path rules
work (e.g. blocking youtube.com/shorts while allowing youtube.com). Forced
safe search is applied to Google, Bing, DuckDuckGo, Yahoo, and Brave.

---

## Config files

```
config/
├── schedule.yaml                  ← main config (hours, budget, limits)
└── lists/
    ├── always/
    │   ├── blocked/               ← domains blocked 24/7 (adult.txt, reddit.txt)
    │   ├── blocked_apps.txt       ← apps killed 24/7
    │   └── urls.txt               ← URL patterns blocked 24/7
    └── work/
        ├── allowed.txt            ← THE whitelist (only these pass in work mode)
        ├── blocked_apps.txt       ← apps killed during work mode
        └── urls.txt               ← URL patterns blocked during work mode
```

---

## SSH setup (already done)

SSH config at `~/.ssh/config`:

```
Host adams-pc
    HostName 192.168.1.81
    User parent
    IdentityFile ~/.ssh/patental_control_key
```

Adam's account (`adam`) has no sudo. The `parent` account has sudo.
Adam cannot stop the daemon, proxy, or SSH server.

---

## Installing on his machine (first time)

```bash
# 1. Copy project to his machine
scp -r ~/Desktop/projectsavingprivateadam parent@192.168.1.81:~/

# 2. SSH in and run installer
ssh adams-pc
cd ~/projectsavingprivateadam
sudo bash install/setup.sh adam

# 3. Verify
./status.sh
```

---

## Setting up git sync (one time, on Adam's machine)

This lets deploy.sh use `git pull` instead of SCP — cleaner and updates
agent code as well as config.

```bash
# On your machine first — push to a private GitHub repo
cd ~/Desktop/projectsavingprivateadam
git init && git add . && git commit -m "initial"
git remote add origin git@github.com:YOURUSERNAME/REPONAME.git
git push -u origin main

# Now SSH into Adam's machine
ssh adams-pc

# Generate a read-only deploy key
sudo -u parent ssh-keygen -t ed25519 -f /home/parent/.ssh/github_deploy -N ""
cat /home/parent/.ssh/github_deploy.pub
# → Copy this output, add it to GitHub: repo → Settings → Deploy keys
#   Read-only, do NOT check write access

# Tell SSH to use this key for GitHub
sudo -u parent bash -c 'cat >> /home/parent/.ssh/config << EOF

Host github.com
    IdentityFile /home/parent/.ssh/github_deploy
    IdentitiesOnly yes
EOF'

# Clone the repo
sudo -u parent git clone git@github.com:YOURUSERNAME/REPONAME.git \
    /home/parent/projectsavingprivateadam
```

After this, `./deploy.sh` on your machine pushes to GitHub and pulls on
Adam's machine in one step.

---

## Adam's status page

He can bookmark `http://localhost:8765` in his browser. Shows:
- Current mode and whether free time is counting
- What's triggering the budget (exact site or app)
- Budget remaining with progress bar
- Per-site and per-app limits
- Ranked list of all domains visited today — school sites marked green,
  unknown sites that triggered free time marked orange ("Tell Dad")
