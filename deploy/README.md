# Put AdRx online as a website

The tool can run as a public website where anyone can check an ad by pasting text or uploading a post, PDF,
poster, voice note or video. On either option below:

- **Quick check only.** The phrase rules run on every check, and AI review is switched off, so it never costs
  you Anthropic credits.
- **Visitor limits.** Each visitor (IP address) gets 20 checks an hour and 60 a day.
- **Privacy.** Uploaded files are deleted as soon as the check is done, and nothing is stored.
- **HTTPS.** Both options give the site a secure address for free.

| | Free on Render | Your own server |
| --- | --- | --- |
| Cost | Free, no card | About ₹500-1,000 a month |
| Address | `https://adrx.onrender.com` | `https://your-name.duckdns.org` or your domain |
| Speed | Text in a second, posters in about 15 s, a 30-second video in a minute or two | Several times faster |
| When idle | Sleeps after 15 minutes without visitors; the next visit waits about 50 s (see below) | Always on |
| Files | Up to 30 MB; first 2 minutes of audio/video | Up to 100 MB; first 3 minutes |
| Speech-to-text | Small Whisper model on the server (good English, weak Hindi); better with a free Groq key | Whisper "small" on the server |

## Option A: free on Render

Render (https://render.com) runs the app for free from your GitHub repository, using `render.yaml` in this
project.

1. **Create the site.** Sign in at https://dashboard.render.com with GitHub. Click **New**, then **Blueprint**,
   and connect your GitHub account if asked. Choose the repository with this project.
2. Render reads `render.yaml` and asks for **OPENAI_API_KEY**. Leave it empty, or paste a free Groq key (see
   below), then click **Deploy Blueprint**.
3. The first build takes about 10 minutes. Then open the address Render shows, for example
   `https://adrx.onrender.com`.

Every change pushed to the repository redeploys the site automatically. To try the live site with the sample
files, open the repository's **Actions** tab, choose **Check the website** and click **Run workflow**.

**Speech-to-text.** Without a key, voice notes and spoken words in videos are turned into text by the small
Whisper "base" model on the server: good for clear English, weak for Hindi and Hinglish. For much better
results, including Hindi, add a free Groq key: sign in at https://console.groq.com/keys, click **Create API
Key**, copy it (it starts with `gsk_`), then in Render open the service's **Environment**, set
`OPENAI_API_KEY` to the key and click **Save, rebuild, and deploy**. Audio is then sent to Groq to be turned
into text, and the page's privacy note says so.

**Sleeping.** Render puts a free site to sleep after 15 minutes without visitors. The GitHub workflow
`.github/workflows/keep-awake.yml` opens the site on a schedule, but GitHub often starts scheduled runs late
(30-70 minutes apart), so the site can still fall asleep. For a site that never sleeps, add a free monitor at
https://uptimerobot.com that opens `https://adrx.onrender.com/api/health` every 5 minutes. Awake all the time,
one site uses about 744 of Render's 750 free hours a month, so don't add a second free service to the same
Render account. If Render gave the site a different address, add a repository variable `SITE_URL` with it
(GitHub: Settings > Secrets and variables > Actions > Variables).

## Option B: your own server

This option uses the files in this folder: Docker, Caddy for HTTPS and a one-command setup script. Files can be
up to 100 MB, and only the first 3 minutes of audio or video are checked. Two files are processed at a time,
and others wait their turn. You only pay for the server.

## 1. Rent a small server (about 10 minutes)

Create a server with **Ubuntu 24.04**, **2 vCPUs** and **at least 2 GB of memory** (4 GB is faster), at any
provider, for example:

- **DigitalOcean**: a Droplet in the Bangalore region.
- **AWS Lightsail**: a Linux instance in the Mumbai region. In the instance's **Networking** tab, add a rule
  that allows **HTTPS (443)**. The login user is `ubuntu`, not `root`.
- **Hetzner Cloud**: a CX22, which is the cheapest of the three.

Write down the server's **IP address** (for example `203.0.113.10`) and its password or SSH key.

## 2. Get a free web address (about 5 minutes)

1. Open https://www.duckdns.org and sign in with Google or GitHub.
2. Type a name, for example `adrx-check`, and click **add domain**.
3. In the **current ip** box next to it, type your server's IP address and click **update ip**.

Your website's address is now `adrx-check.duckdns.org`. If you skip this step, the site still works, but only
over plain http at `http://<server IP>`. That's fine for a first test, but not for real use.

## 3. Copy the tool to the server

In **PowerShell** on your PC (use `ubuntu@` instead of `root@` on AWS Lightsail):

```powershell
scp "$HOME\Downloads\nmc-compliance-checker.zip" root@203.0.113.10:~/
ssh root@203.0.113.10
```

The first time, type `yes`, then the server's password.

## 4. Start the website (one command)

You are now logged in to the server. Run:

```bash
sudo python3 -m zipfile -e nmc-compliance-checker.zip /opt/
sudo bash /opt/nmc-compliance-checker/deploy/setup-server.sh adrx-check.duckdns.org
```

The first run takes about 10 minutes. It installs Docker, builds the app, downloads the speech model and gets
the HTTPS certificate. It ends with:

```text
AdRx is online: https://adrx-check.duckdns.org
```

Open that address in your browser and share it.

## Update to a new version

Copy the new zip to the server (step 3), then run:

```bash
sudo python3 -m zipfile -e nmc-compliance-checker.zip /opt/
sudo bash /opt/nmc-compliance-checker/deploy/setup-server.sh
```

Your settings and the web address stay as they are.

## Change a setting

The settings are in `/opt/nmc-compliance-checker/deploy/.env`. You can open it with
`sudo nano /opt/nmc-compliance-checker/deploy/.env`.

| Setting | Default | What it does |
| --- | --- | --- |
| `RATE_LIMIT_PER_HOUR`, `RATE_LIMIT_PER_DAY` | 20, 60 | Checks per visitor. |
| `MAX_UPLOAD_MB` | 100 | Largest file. If you change it, keep `PROXY_MAX_BODY` a little higher. |
| `MAX_MEDIA_SECONDS` | 180 | How much of each recording is checked. |
| `MAX_PARALLEL_CHECKS` | 2 | Files processed at the same time. Use 1 on a 2 GB server. |
| `WHISPER_MODEL` | small | Use `large-v3-turbo` for better Hindi. It needs 4 GB of memory and is slower. |

Apply your changes:

```bash
cd /opt/nmc-compliance-checker/deploy && sudo docker compose up -d --build
```

## Use your own domain later

At your domain provider, add an **A record** that points to the server's IP address (for example
`check.yourclinic.in -> 203.0.113.10`). Then run:

```bash
sudo bash /opt/nmc-compliance-checker/deploy/setup-server.sh check.yourclinic.in
```

## Useful commands (run them in `/opt/nmc-compliance-checker/deploy`)

| What | Command |
| --- | --- |
| See the latest log lines | `sudo docker compose logs --tail 100 app` |
| Restart the website | `sudo docker compose restart` |
| Stop the website | `sudo docker compose down` |
| Start it again | `sudo docker compose up -d` |

## If something doesn't work

- **The page doesn't open:** ports 80 and 443 must be open in your provider's firewall (on AWS Lightsail,
  use the Networking tab).
- **The setup says "points to ... not to this server":** fix the IP address in DuckDNS. The certificate
  follows within a few minutes, without running anything again.
- **Anything else:** send the output of `sudo docker compose logs --tail 100`.

## Good to know

- **Why AI review is off:** on a public website, every visitor's check would be billed to your Anthropic key.
  Turn it on (`AI_REVIEW=on` plus `ANTHROPIC_API_KEY=` in `deploy/.env`) only if you add your own way to
  control who uses it.
- **Memory:** on a 2 GB server, the setup adds 2 GB of swap, so the speech model fits. Checks are slower
  there than on a 4 GB server.
- **Restarts:** the site starts again by itself after a server restart.
