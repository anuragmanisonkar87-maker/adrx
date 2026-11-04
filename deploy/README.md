# Put AdRx online as a website

The tool can run as a website where the people you give an account can check an ad by pasting text or
uploading a post, PDF, poster, voice note or video. On either option below:

- **Sign-in.** Every check needs an account. You add accounts on the site's **Users** page and choose what
  each one may check (see "Sign-in and user accounts" in the main README).
- **Quick check only.** The phrase rules run on every check, and AI review is switched off, so it never costs
  you Anthropic credits.
- **Limits.** Each account gets 20 checks an hour and 60 a day.
- **Privacy.** Uploaded files are deleted as soon as the check is done. Only the accounts and a log of
  sign-ins and changes are kept.
- **HTTPS.** Both options give the site a secure address for free.

| | Free on Render | Your own server |
| --- | --- | --- |
| Cost | Free, no card | About ₹500-1,000 a month |
| Address | `https://adrx.onrender.com` | `https://your-name.duckdns.org` or your domain |
| Speed | Text in a second, posters in about 15 s, a 30-second video in a minute or two | Several times faster |
| When idle | Keeps itself awake (see below) | Always on |
| Files | Up to 250 MB; first 2 minutes of audio/video | Up to 100 MB; first 3 minutes |
| Speech-to-text | Whisper "base" on the server (good English, weak Hindi); with a free Groq key, Whisper large-v3 (accurate Hindi and Hinglish) | Whisper "small" on the server |

## Option A: free on Render, with accounts in Supabase

Render (https://render.com) runs the app for free from your GitHub repository, using `render.yaml` in this
project. The accounts are kept in a free Supabase database (https://supabase.com), because Render's free plan
starts the server with an empty disk at every deploy and restart.

1. **Create the database.** Sign in at https://supabase.com/dashboard and click **New project**. Name it
   `adrx`, click **Generate a password** and keep the password somewhere safe, choose the region
   **Southeast Asia (Singapore)** (where the Render site runs), and click **Create new project**.
2. **Copy its address.** When the project is ready, click **Connect** at the top of the project page, then
   **Direct**, and choose **Session pooler**. Copy the connection string as it is. It looks like
   `postgresql://postgres.abcdefgh:[YOUR-PASSWORD]@aws-0-ap-southeast-1.pooler.supabase.com:5432/postgres`.
   Use the session pooler, not the direct connection, which Render can't reach. (Forgot the database password?
   Supabase can't show it again: **Project Settings > Database > Reset password** sets a new one.)
3. **Create the site.** Sign in at https://dashboard.render.com with GitHub. Click **New**, then **Blueprint**,
   and connect your GitHub account if asked. Choose the repository with this project. Render reads
   `render.yaml` and asks for these values:
   - **ADMIN_PASSWORD**: the first password of the `admin` account, at least 10 characters.
   - **DATABASE_URL**: the connection string from step 2, exactly as copied (with `[YOUR-PASSWORD]`).
   - **DATABASE_PASSWORD**: the database password. The app puts it in place of `[YOUR-PASSWORD]`.
   - **OPENAI_API_KEY**: leave it empty, or paste a free Groq key (see below).

   Then click **Deploy Blueprint**.
4. The first build takes about 10 minutes. Then open the address Render shows, for example
   `https://adrx.onrender.com`. Sign in as `admin`, and add accounts on the **Users** page.

If the site was created before sign-in existed, add `ADMIN_PASSWORD`, `DATABASE_URL` and `DATABASE_PASSWORD` on the service's
**Environment** page instead: click **Edit**, then **Add variable** for each one, then **Save, rebuild, and
deploy**.

The tables go into their own schema, `adrx`, which Supabase's public Data API doesn't publish, and row-level
security is switched on for them as well. Supabase pauses a free project after about a week with little use.
The site runs a small clean-up in the database a few times a day, which should keep it active. If the project
is paused anyway, sign-in stops working until you open it in Supabase and click **Resume project**.

Every change pushed to the repository then redeploys the site by itself, because Render's GitHub app tells
Render about each push. If you created the site from a public repository address instead, Render can't see
your pushes. To fix that, open the service's **Settings**, click **Edit** next to **Source**, click **GitHub**
under **Git Provider**, let Render use the repository on GitHub, then choose the repository and save.

To try the live site with the sample files, open the repository's **Actions** tab, choose **Check the
website** and click **Run workflow**. With sign-in on, it checks that the site refuses visitors without an
account. To have it check the sample files too, add an account for it (it needs text, documents, audio and
video; untick "Ask them to choose their own password"). Then save the username and password as the repository
secrets `ADRX_USERNAME` and `ADRX_PASSWORD` (GitHub: Settings > Secrets and variables > Actions).

**Speech-to-text.** Without a key, voice notes and spoken words in videos are turned into text by the small
Whisper "base" model on the server: good for clear English, weak for Hindi and Hinglish. For accurate Hindi
and Hinglish, add a free Groq key, and Groq's Whisper large-v3 model is used instead:

1. Sign in at https://console.groq.com/keys, click **Create API Key**, name it `adrx` and copy the key (it
   starts with `gsk_` and is shown only once).
2. In Render, open the service's **Environment** page and click **Edit**, then **Add variable**. Type
   `OPENAI_API_KEY` as the name and paste the key as the value.
3. Click the arrow next to **Save, rebuild, and deploy** and choose **Save and deploy** (no rebuild needed).

Audio is then sent to Groq to be turned into text, and the page's privacy note says so. Groq's free plan has
hourly and daily limits on audio, far more than a test site needs; if one is reached, speech is skipped until
it resets.

**Sleeping.** Render puts a free site to sleep after 15 minutes without visitors, and the next visitor then
waits about a minute. This site keeps itself awake: with `KEEP_AWAKE=on` (set in `render.yaml`) the app opens
its own address every 10 minutes. To check, open `https://adrx.onrender.com/api/health`: it shows
`"keep_awake": true`. Awake all month, the site uses about 744 of the 750 free hours a Render workspace gets,
so don't run a second free web service in the same workspace, or set `KEEP_AWAKE` to `off` to let this one
sleep. The GitHub workflow `.github/workflows/keep-awake.yml` is a backup that wakes the site if it ever does
fall asleep. If Render gave the site a different address, add a repository variable `SITE_URL` with it
(GitHub: Settings > Secrets and variables > Actions > Variables).

## Option B: your own server

This option uses the files in this folder: Docker, Caddy for HTTPS and a one-command setup script. Files can be
up to 100 MB, and only the first 3 minutes of audio or video are checked. Two files are processed at a time,
and others wait their turn. You only pay for the server. The accounts are kept on the server, in the app's
data volume, which survives updates. To keep them in Supabase instead, put its connection string in
`DATABASE_URL` in `deploy/.env` (see Option A, steps 1 and 2).

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
Sign in as: admin
Password:   (a random password)
```

Open that address, sign in as `admin` with that password, and change it on the **Password** page. Then add
accounts for your colleagues on the **Users** page. The first password is also saved in `deploy/.env`
(`ADMIN_PASSWORD`).

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
| `RATE_LIMIT_PER_HOUR`, `RATE_LIMIT_PER_DAY` | 20, 60 | Checks per account. |
| `REQUIRE_LOGIN` | on | Every check needs an account. `off` opens the site to anyone with the link. |
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

- **Why AI review is off:** every AI check is billed to your Anthropic key. To offer it, set `AI_REVIEW=on` and
  `ANTHROPIC_API_KEY=` in `deploy/.env`, then tick **AI review** only for the accounts that should have it.
- **Forgot the admin password:** run
  `cd /opt/nmc-compliance-checker/deploy && sudo docker compose exec app python -m app.accounts reset-password admin`.
- **Memory:** on a 2 GB server, the setup adds 2 GB of swap, so the speech model fits. Checks are slower
  there than on a 4 GB server.
- **Restarts:** the site starts again by itself after a server restart.
