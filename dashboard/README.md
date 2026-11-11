# The web panel

A static page with no build step and no libraries: `index.html`, `assets/app.css`, `assets/app.js`.
It only reads data and shows it; it never sends anything to Instagram.

Sections: Overview, Reels (each reel: keyword, typos, link, message text, A/B variants with a funnel per
variant, and a phone preview of what people see), People, Analytics, Settings. English and Russian (switch in
the sidebar or in Settings), light and dark theme (follows your system, or pick one).

## Two modes

**Real data.** Served by `autoreply/web.py` at `/panel/`, behind a login (`PANEL_USER` and `PANEL_PASSWORD` in
`state/.env`; with either missing, the panel answers 403 to everyone). `app.js` then reads `data/report.json`,
which `autoreply/panel.py` builds from the bot's database on every request. Settings are shown read-only:
you change them by editing `config.json` (or by asking your coding agent). "Pressed the button" counts taps on
the button in the DM; "Clicked the link" counts people who opened a tracked link (`/r/…`). Link previews that
Instagram's robot fetches are not counted as clicks.

**Demo.** Any other address, or `/panel/?demo=…`: reads invented data from `data/demo.json` (Russian) or
`data/demo.en.json` (English), built by `tools/make_demo.py`. Buttons only show a "Demo: …" note.

Try the demo locally:

```bash
python3 -m http.server 8765 --bind 127.0.0.1 --directory dashboard
# then open http://127.0.0.1:8765/?lang=en
```

Useful addresses: `?demo=ok`, `?demo=attention`, `?demo=blocked` (put them before `#`), `?lang=en` or `?lang=ru`,
and a person's card: `#/people?person=<comment_id>`.
