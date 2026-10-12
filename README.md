# League Record Book

A static site showing your ESPN fantasy football league's history: average finish, head-to-head
records, wins above expected, playoffs, last places, mid-game injuries, start/sit accuracy, and
draft hits and busts.

- `docs/index.html`: the page. It loads `docs/league.json`, or shows a sample league if that file is missing.
- `fetch_league.py`: pulls your league from ESPN and writes `docs/league.json`.
- `.github/workflows/update-league.yml`: reruns the script every Tuesday and commits the new data.

## 1. Find your league ID (and cookies, for private leagues)

- **League ID**: open your league on espn.com. The address contains `leagueId=1234567`. That number is it.
- **Private leagues** also need two cookies from your signed-in browser:
  Chrome → View → Developer → Developer Tools → Application tab → Cookies → `https://www.espn.com`.
  Copy the values of `espn_s2` and `SWID` exactly as shown. Older accounts' SWID has curly braces around it; newer ones don't.
  These are your login. Don't commit them or paste them anywhere public.

## 2. Run it locally

```
pip install -r requirements.txt
ESPN_S2='...' ESPN_SWID='{...}' python fetch_league.py --league-id 1234567
cd docs && python -m http.server 8000
```

Then open http://localhost:8000. (Opening index.html straight from disk won't load league.json.)

## 3. Host it on GitHub Pages

1. Create a GitHub repo and push this folder.
2. Settings → Secrets and variables → Actions:
   - Variables: `ESPN_LEAGUE_ID`
   - Secrets: `ESPN_S2`, `ESPN_SWID` (private leagues only)
3. Settings → Pages → Deploy from a branch → `main`, folder `/docs`.
4. Actions tab → "Update league data" → Run workflow.

The site will be at `https://<your-username>.github.io/<repo-name>/`. A GitHub Pages site is public
on free plans, so anyone with the link can see manager names and records.

ESPN cookies usually last about a year. If the workflow starts failing with a 401, copy fresh ones
into the secrets.
