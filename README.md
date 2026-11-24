# Futures Board: Kalshi line movement for NFL, NBA, and NHL

This board tracks Kalshi futures and award markets for NFL, NBA, and NHL. It
updates every hour on GitHub's servers and runs on a private-link web page you
can open from Windows, Mac, or your phone. It's free and doesn't need an API key,
because Kalshi's market-data endpoints are public.

**What it tracks:** championships (Super Bowl, Stanley Cup, NBA Finals),
conference and division winners, make or miss playoffs, and the major awards
(MVP, ROY, DPOY, Hart, Vezina, Norris, Calder, Conn Smythe, and so on). The
collector finds these series on its own each run, so markets Kalshi adds
mid-season show up automatically.

**What each row shows:** the mid probability, American odds, no-vig odds
(normalized across the event), the real cost to buy Yes including Kalshi's taker
fee (as American odds), and the moves over 1h, 24h, and 7d. Moves are
color-coded by size: blue when the line shortens, red when it drifts. Each row
also has a 7-day sparkline, and clicking it opens the full chart.

---

## One-time setup (about 10 minutes, in a web browser)

1. **Create a GitHub account** at github.com if you don't have one. The free plan is fine.

2. **Create a repository.** Click **+** (top right), then **New repository**.
   - Name: `futures-board`
   - Set it to **Public**. Free GitHub Pages hosting needs a public repo. The only
     data in it is public Kalshi prices. The URL won't be listed anywhere, but
     anyone who has it can view it.
   - Click **Create repository**.

3. **Upload the files.** On the new repo page, click **uploading an existing file**.
   Unzip `futures-board.zip` and drag in everything inside it: `index.html`,
   `README.md`, and the `collector`, `data`, and `setup` folders. Then click
   **Commit changes**.

4. **Add the hourly job.** Click **Add file**, then **Create new file**.
   - In the name box, type exactly: `.github/workflows/collect.yml`
   - Open `setup/collect.yml` from the zip, copy everything, and paste it in.
   - Click **Commit changes**.

5. **Turn on the website.** Go to **Settings**, then **Pages**. Under **Source**,
   choose **GitHub Actions**.

6. **Run it once with history backfill.** Go to the **Actions** tab (click
   "I understand… enable" if asked). Open **Hourly Kalshi snapshot**, click
   **Run workflow**, set *backfill_days* to `30`, and click **Run workflow**.
   This pulls 30 days of Kalshi candlestick history, so the 24h and 7d movers
   work right away.

7. **Open your board** at `https://<your-github-username>.github.io/futures-board/`.
   Bookmark it on both computers and your phone.

After that, it runs by itself every hour at about :07 past.

> If step 6 fails at "Save snapshot to repo", go to **Settings**, then
> **Actions**, then **General**, then **Workflow permissions**, select
> **Read and write permissions**, save, and run it again.

---

## Tuning what's on the board

Edit `collector/config.json` right on GitHub (click the file, then the pencil
icon). Changes apply on the next hourly run.

- `include_series` / `exclude_series`: force a Kalshi series ticker in or out
  (for example `"KXNFLMVP"`). The ticker is in the URL on kalshi.com.
- `include_keywords` / `exclude_keywords`: the rules the auto-discovery uses.
- `kinds`: drop a whole category, such as `"Division"`.
- `kalshi_fee_rate`: used for the "Buy (w/ fee)" column. The default 0.07
  matches Kalshi's standard taker fee of 0.07 × P × (1 − P) per contract.

To see what the discovery picked up and what it skipped (and why), open
`data/catalog.json` in the repo.

## How it works

- `collector/kalshi_board.py` lists Kalshi's Sports series and keeps the NFL,
  NBA, and NHL futures and awards. It pulls every open market's bid, ask, and
  last price, then stores the mid. When the spread is wider than 10¢ it uses
  the last trade instead, which keeps illiquid longshots from jumping around.
- History lives in `data/history/<SERIES>.json`: hourly points for 21 days,
  then one point per day after that, so files stay small all season.
- `data/latest.json` is what the page loads. It includes precomputed moves and
  sparklines.
- The workflow commits the new snapshot and republishes the page every hour.

## Things to know

- GitHub's scheduler sometimes runs a few minutes late, and occasionally skips
  a run when it's busy. The board shows when it last updated and turns the dot
  amber if the data is more than 2 hours old.
- GitHub pauses scheduled jobs in repos with no activity for 60 days. The
  hourly commits count as activity, so this shouldn't happen. If it does,
  Actions shows an **Enable workflow** button.
- American odds are converted from the raw Kalshi price, which still includes
  the vig. Use the **No-vig** column when comparing against a sportsbook's
  fair line, and the **Buy** column for what you'd actually pay on Kalshi.
