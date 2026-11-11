#!/usr/bin/env python3
"""Generates the README infographics in docs/img/ (standard library only).

    python3 docs/make_images.py

Logos: Simple Icons (CC0), icons: Lucide (ISC); both vendored in docs/icons/ (see docs/icons/NOTICE.md).
Every number and every behaviour shown here comes from the code, SPEC.md or a measurement; the comments
next to each figure say where.
"""
import html
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ICONS = os.path.join(HERE, "icons")
OUT = os.path.join(HERE, "img")

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Inter, Helvetica, Arial, sans-serif"
MONO = "SFMono-Regular, Menlo, Consolas, 'Liberation Mono', monospace"
INK, MUTED, FAINT, LINE, CARD, SOFT = "#0f172a", "#475569", "#94a3b8", "#e2e8f0", "#ffffff", "#f8fafc"
PINK, PINK_SOFT, GREEN, GREEN_SOFT, AMBER, AMBER_SOFT, RED, RED_SOFT, BLUE = (
    "#dd2a7b", "#fdf2f8", "#16a34a", "#f0fdf4", "#d97706", "#fffbeb", "#dc2626", "#fef2f2", "#2563eb")

BRAND = {"redis": "#FF4438", "postgresql": "#4169E1", "python": "#3776AB", "sqlite": "#003B57",
         "instagram": "#E4405F", "telegram": "#26A5E4", "nextdotjs": "#000000", "nodedotjs": "#5FA04E",
         "docker": "#2496ED", "cloudflare": "#F38020", "letsencrypt": "#003A70", "meta": "#0467DF"}

ALLOWED = ("path", "rect", "circle", "line", "polyline", "polygon", "ellipse")


def esc(s):
    return html.escape(str(s), quote=True)


def _read(name):
    with open(os.path.join(ICONS, name), encoding="utf-8") as f:
        return f.read()


def logo(slug, x, y, size, color=None):
    """A Simple Icons logo (24x24 viewBox, one path), filled."""
    d = re.search(r'<path d="([^"]+)"', _read(f"si-{slug}.svg")).group(1)
    s = size / 24
    return (f'<path transform="translate({x},{y}) scale({s:.4f})" d="{esc(d)}" '
            f'fill="{color or BRAND[slug]}"/>')


def icon(name, x, y, size, color, width=2):
    """A Lucide icon (24x24 viewBox, stroked)."""
    body = _read(f"lu-{name}.svg")
    parts = re.findall(r"<(%s)\b([^>]*)/>" % "|".join(ALLOWED), body)
    inner = "".join(f"<{tag}{attrs}/>" for tag, attrs in parts)
    s = size / 24
    return (f'<g transform="translate({x},{y}) scale({s:.4f})" fill="none" stroke="{color}" '
            f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round">{inner}</g>')


def text(x, y, s, size=16, color=INK, weight=400, anchor="start", font=FONT, extra=""):
    return (f'<text x="{x}" y="{y}" font-family="{font}" font-size="{size}" fill="{color}" '
            f'font-weight="{weight}" text-anchor="{anchor}" {extra}>{esc(s)}</text>')


def svg(w, h, body, defs=""):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">'
            f'<defs>{defs}</defs>{body}</svg>\n')


def card(w, h, fill=CARD):
    return f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="22" fill="{fill}" stroke="{LINE}"/>'


def box(x, y, w, h, fill=SOFT, stroke=LINE, rx=14, sw=1, dash=""):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{d}/>'


def arrow(x1, y1, x2, y2, color=FAINT, width=2, dash="", marker="ah"):
    d = f' stroke-dasharray="{dash}"' if dash else ""
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{width}"{d} '
            f'marker-end="url(#{marker})"/>')


def markers():
    out = ""
    for mid, col in (("ah", FAINT), ("ahp", PINK), ("ahr", RED), ("ahg", GREEN)):
        out += (f'<marker id="{mid}" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" markerHeight="7" '
                f'orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="{col}"/></marker>')
    return out


def write(name, content):
    with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
        f.write(content)


# ---------------------------------------------------------------- hero
def hero():
    W, H = 1200, 540
    defs = (
        '<linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#120a2b"/>'
        '<stop offset="0.6" stop-color="#2a0c33"/><stop offset="1" stop-color="#43102c"/></linearGradient>'
        '<linearGradient id="ig" x1="0" y1="1" x2="1" y2="0"><stop offset="0" stop-color="#f58529"/>'
        '<stop offset="0.5" stop-color="#dd2a7b"/><stop offset="1" stop-color="#8134af"/></linearGradient>'
        '<radialGradient id="glow" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="#dd2a7b" stop-opacity="0.35"/>'
        '<stop offset="1" stop-color="#dd2a7b" stop-opacity="0"/></radialGradient>'
        + markers())
    b = [f'<rect width="{W}" height="{H}" rx="26" fill="url(#bg)"/>',
         '<circle cx="1000" cy="250" r="300" fill="url(#glow)"/>']
    b.append(text(64, 128, "tinyreply", 88, "#ffffff", 800, extra='letter-spacing="-2"'))
    b.append('<rect x="68" y="150" width="160" height="7" rx="3.5" fill="url(#ig)"/>')
    b.append(text(64, 212, "Comment a keyword, get the link in DM.", 32, "#f5f0ff", 600))
    b.append(text(64, 252, "A self-hosted Instagram autoresponder that fits", 22, "#c8bddc"))
    b.append(text(64, 282, "on the smallest VPS, built on Meta's official API.", 22, "#c8bddc"))
    chips = [("cpu", "32 MB", "RAM on a live server"), ("zap", "0", "dependencies"),
             ("shield-check", "0", "open ports"), ("check", "1,089", "tests")]
    x = 64
    for name, big, small in chips:
        w = 158
        b.append(f'<rect x="{x}" y="338" width="{w}" height="122" rx="16" fill="#ffffff" fill-opacity="0.07" '
                 f'stroke="#ffffff" stroke-opacity="0.16"/>')
        b.append(icon(name, x + 18, 356, 24, "#f9a8d4"))
        b.append(text(x + 18, 418, big, 34, "#ffffff", 800))
        b.append(text(x + 18, 444, small, 14, "#c8bddc"))
        x += w + 14

    # phone mock-up: the comment, the public reply, the DM (example texts, not real people)
    px, py, pw, ph = 846, 40, 290, 460
    b.append(f'<rect x="{px}" y="{py}" width="{pw}" height="{ph}" rx="38" fill="#0b0b10" stroke="#3b3b48" stroke-width="2"/>')
    b.append(f'<rect x="{px + 10}" y="{py + 10}" width="{pw - 20}" height="{ph - 20}" rx="30" fill="#ffffff"/>')
    b.append(f'<rect x="{px + pw / 2 - 40}" y="{py + 18}" width="80" height="20" rx="10" fill="#0b0b10"/>')
    b.append(logo("instagram", px + 28, py + 52, 22))
    b.append(text(px + 58, py + 69, "Comments", 15, INK, 700))
    # comment
    cy = py + 104
    b.append(f'<circle cx="{px + 42}" cy="{cy + 14}" r="14" fill="#fbcfe8"/>')
    b.append(text(px + 66, cy + 10, "ann", 13, INK, 700))
    b.append(text(px + 66, cy + 30, "guide", 15, INK))
    # public reply
    ry = cy + 52
    b.append(f'<circle cx="{px + 70}" cy="{ry + 12}" r="12" fill="url(#ig)"/>')
    b.append(text(px + 90, ry + 8, "your_account", 12, INK, 700))
    b.append(text(px + 90, ry + 27, "Sent it to your DMs!", 14, MUTED))
    # divider + DM
    dy = ry + 66
    b.append(f'<line x1="{px + 28}" y1="{dy}" x2="{px + pw - 28}" y2="{dy}" stroke="{LINE}"/>')
    b.append(logo("instagram", px + 28, dy + 16, 18, INK))
    b.append(text(px + 54, dy + 31, "Direct · your_account", 13, INK, 700))
    by = dy + 52
    b.append(f'<rect x="{px + 26}" y="{by}" width="214" height="118" rx="18" fill="#efeff4"/>')
    b.append(text(px + 42, by + 28, "Hi! Here is the guide:", 14, INK))
    b.append(text(px + 42, by + 52, "example.com/guide", 14, BLUE, 600))
    b.append(text(px + 42, by + 80, "Follow for more:", 14, INK))
    b.append(text(px + 42, by + 102, "instagram.com/your_account", 12, BLUE))
    b.append(text(px + 30, by + 146, "arrives a minute or two after the comment", 12, FAINT))
    write("hero.svg", svg(W, H, "".join(b), defs))


# ---------------------------------------------------------------- flow
def flow():
    W, H = 1200, 560
    b = [card(W, H)]
    b.append(text(44, 58, "One cycle, every 90 seconds", 26, INK, 800))
    b.append(text(44, 88, "Outbound HTTPS only. Each step is committed to SQLite before the next one starts.", 16, MUTED))
    # SPEC 8.1-8.4, 7.2-7.4; default poll_interval_sec 90 (config.example.json)
    steps = [("message-circle", "Read", ["comments of every", "active reel (7 days)"]),
             ("spell-check", "Match", ["keyword rules,", "questions skipped"]),
             ("lock", "Claim", ["written to the DB", "before the request"]),
             ("send", "Private reply", ["the DM with your link,", "≤ N per hour, spaced"]),
             ("reply", "Public reply", ["“check your DMs”", "under the comment"]),
             ("bell", "Report", ["journal, statistics,", "Telegram alerts"])]
    n = len(steps)
    x0, gap, cy = 110, 196, 196
    for i, (ic, title, lines) in enumerate(steps):
        cx = x0 + i * gap
        hot = ic in ("lock", "send")
        ring = PINK if hot else "#cbd5e1"
        fill = PINK_SOFT if hot else SOFT
        if i < n - 1:
            b.append(arrow(cx + 44, cy, cx + gap - 48, cy, PINK if hot and ic == "lock" else FAINT, 2.5,
                           marker="ahp" if ic == "lock" else "ah"))
        b.append(f'<circle cx="{cx}" cy="{cy}" r="40" fill="{fill}" stroke="{ring}" stroke-width="2.5"/>')
        b.append(icon(ic, cx - 15, cy - 15, 30, PINK if hot else INK))
        b.append(f'<circle cx="{cx + 30}" cy="{cy - 30}" r="13" fill="{INK}"/>')
        b.append(text(cx + 30, cy - 25, str(i + 1), 14, "#ffffff", 700, "middle"))
        b.append(text(cx, cy + 72, title, 18, INK, 700, "middle"))
        for k, line in enumerate(lines):
            b.append(text(cx, cy + 96 + 20 * k, line, 14, MUTED, 400, "middle"))
    # outcome branches under step 4 (SPEC 6.1 classify + 8.5 holds)
    sx = x0 + 3 * gap
    b.append(f'<path d="M{sx},{cy + 140} L{sx},{cy + 168}" stroke="{FAINT}" stroke-width="2"/>')
    b.append(f'<path d="M{sx - 300},{cy + 168} L{sx + 300},{cy + 168}" stroke="{FAINT}" stroke-width="2"/>')
    branches = [
        (sx - 300, GREEN, GREEN_SOFT, "check", "Meta confirms", ["Marked done. Next person."]),
        (sx, AMBER, AMBER_SOFT, "rotate-ccw", "Never left the machine", ["Back in the queue,", "tried next cycle."]),
        (sx + 300, RED, RED_SOFT, "triangle-alert", "Unclear answer", ["Marked uncertain, never sent", "again; pause + alert."]),
    ]
    for bx, col, soft, ic, title, lines in branches:
        b.append(arrow(bx, cy + 168, bx, cy + 196, col, 2, marker={GREEN: "ahg", RED: "ahr"}.get(col, "ah")))
        b.append(box(bx - 135, cy + 200, 270, 104, soft, col, 14, 1.5))
        b.append(icon(ic, bx - 117, cy + 216, 22, col))
        b.append(text(bx - 86, cy + 234, title, 16, INK, 700))
        for k, line in enumerate(lines):
            b.append(text(bx - 117, cy + 264 + 20 * k, line, 14, MUTED))
    b.append(text(44, H - 26, "Rule of thumb: a missed DM can be fixed by hand, a second DM cannot be taken back.", 14, FAINT))
    write("flow.svg", svg(W, H, "".join(b), markers()))


# ---------------------------------------------------------------- architecture
def architecture():
    W, H = 1200, 560
    b = [card(W, H)]
    b.append(f'<line x1="600" y1="40" x2="600" y2="{H - 40}" stroke="{LINE}" stroke-width="2"/>')

    def node(x, y, w, label, sub, mark, fill=SOFT, stroke=LINE, sw=1):
        out = box(x, y, w, 64, fill, stroke, 14, sw)
        out += mark(x + 16, y + 18)
        out += text(x + 56, y + 30, label, 16, INK, 700)
        out += text(x + 56, y + 50, sub, 13, MUTED)
        return out

    L = lambda slug: (lambda x, y: logo(slug, x, y, 28))
    I = lambda name, col=INK: (lambda x, y: icon(name, x, y, 28, col))
    # left: what a typical clone needs (OpenReply: README/docker-compose; ChatbotX: docs)
    b.append(text(40, 66, "A typical open-source ManyChat clone", 22, INK, 800))
    b.append(text(40, 92, "Built for cloud hosting and agencies", 15, MUTED))
    b.append(node(40, 120, 240, "Public HTTPS", "domain + TLS certificate", L("letsencrypt")))
    b.append(node(320, 120, 240, "Web app", "Next.js (Node)", L("nextdotjs")))
    b.append(node(320, 214, 240, "Queue worker", "Node", L("nodedotjs")))
    b.append(node(40, 308, 160, "PostgreSQL", "database", L("postgresql")))
    b.append(node(220, 308, 150, "Redis", "queue", L("redis")))
    b.append(node(390, 308, 170, "SMTP", "e-mail login", I("mail")))
    b.append(arrow(280, 152, 314, 152))
    b.append(text(60, 206, "Meta webhooks come in here", 13, FAINT))
    b.append(arrow(440, 184, 440, 208))
    for sx_, tx in ((360, 120), (440, 295), (520, 475)):
        b.append(arrow(sx_, 278, tx, 302))
    b.append(box(40, 410, 520, 100, "#fff7ed", "#fed7aa", 14))
    for k, line in enumerate(["4–10 processes to keep alive and update",
                              "a build step that needs 1–2 GB of RAM",
                              "a stable public URL: if it changes, OAuth and webhooks break"]):
        b.append(icon("x", 58, 424 + 26 * k, 18, AMBER, 2.5))
        b.append(text(86, 438 + 26 * k, line, 14, INK))

    # right: tinyreply
    b.append(text(640, 66, "tinyreply", 22, PINK, 800))
    b.append(text(640, 92, "Built for one creator on one small server", 15, MUTED))
    b.append(box(640, 140, 260, 120, PINK_SOFT, PINK, 16, 2))
    b.append(logo("python", 660, 160, 36))
    b.append(text(708, 176, "bot.py run", 18, INK, 700, font=MONO))
    b.append(text(708, 200, "1 process · 1 thread", 14, MUTED))
    b.append(text(708, 222, "Python standard library", 14, MUTED))
    b.append(text(708, 244, "only", 14, MUTED))
    b.append(node(640, 300, 260, "core.db", "one SQLite file (WAL)", L("sqlite")))
    b.append(arrow(770, 260, 770, 294))
    b.append(node(960, 140, 200, "Instagram API", "graph.instagram.com", L("instagram")))
    b.append(node(960, 240, 200, "Telegram", "alerts to you", L("telegram")))
    b.append(arrow(900, 180, 954, 172, PINK, 2.5, marker="ahp"))
    b.append(arrow(900, 220, 954, 266, PINK, 2.5, marker="ahp"))
    b.append(text(1060, 330, "outbound only", 13, PINK, 600, "middle"))
    b.append(box(640, 410, 520, 100, GREEN_SOFT, "#bbf7d0", 14))
    for k, line in enumerate(["no listener: nothing on the internet can talk to it",
                              "no build step, no Docker, no domain, no certificate",
                              "copy 9 files, add a systemd unit, done"]):
        b.append(icon("check", 658, 424 + 26 * k, 18, GREEN, 2.5))
        b.append(text(686, 438 + 26 * k, line, 14, INK))
    write("architecture.svg", svg(W, H, "".join(b), markers()))


# ---------------------------------------------------------------- footprint
def footprint():
    W, H = 1200, 600
    b = [card(W, H)]
    b.append(text(44, 60, "Footprint", 26, INK, 800))
    b.append(text(44, 90, "What it takes to keep the bot running.", 16, MUTED))
    # big number panel; 32 MB = VmRSS of the live systemd service, Ubuntu 24.04, 10 Nov 2026
    b.append(box(44, 120, 330, 420, PINK_SOFT, "#fbcfe8", 18))
    b.append(icon("cpu", 72, 146, 30, PINK))
    b.append(text(68, 280, "32", 120, PINK, 800, extra='letter-spacing="-4"'))
    b.append(text(228, 280, "MB", 40, PINK, 800))
    b.append(text(72, 312, "RAM of the live service", 18, INK, 700))
    b.append(text(72, 334, "(VmRSS, Ubuntu 24.04, Nov 2026)", 14, MUTED))
    b.append(f'<line x1="72" y1="356" x2="346" y2="356" stroke="#fbcfe8"/>')
    # a 1 GB box holds 32 copies: draw a 4x8 grid of tiny squares
    b.append(text(72, 384, "A 1 GB VPS could hold", 16, INK))
    b.append(text(72, 406, "32 of them:", 16, INK, 700))
    for i in range(32):
        gx, gy = 72 + (i % 8) * 32, 424 + (i // 8) * 26
        b.append(f'<rect x="{gx}" y="{gy}" width="24" height="18" rx="4" fill="{PINK}" fill-opacity="{0.9 if i == 0 else 0.25}"/>')

    # bars; scale 0..2048 MB over 560 px
    x0, scale = 586, 560 / 2048
    b.append(text(410, 140, "RAM needed", 18, INK, 700))
    gb = x0 + 1024 * scale
    b.append(f'<line x1="{gb}" y1="152" x2="{gb}" y2="420" stroke="{FAINT}" stroke-dasharray="6 6"/>')
    b.append(text(gb + 6, 166, "1 GB = a typical $3–5 VPS", 13, MUTED))
    rows = [("python", "tinyreply", "measured", 32, 32, PINK, "32 MB"),
            ("nextdotjs", "OpenReply", "estimate: web, worker, Postgres, Redis", 350, 700, "#94a3b8", "≈ 350–700 MB"),
            ("docker", "ChatbotX", "documented minimum; issues report 3–6 GB", 2048, 2048, "#94a3b8", "2 GB +")]
    y = 196
    for slug, name, note, lo, hi, col, label in rows:
        b.append(logo(slug, 410, y - 4, 26, PINK if name == "tinyreply" else None))
        b.append(text(446, y + 12, name, 17, INK, 700))
        b.append(text(446, y + 32, note if len(note) < 24 else "", 12, MUTED))
        b.append(f'<rect x="{x0}" y="{y - 6}" width="{max(lo * scale, 10):.1f}" height="36" rx="8" fill="{col}"/>')
        if hi > lo:
            b.append(f'<rect x="{x0 + lo * scale:.1f}" y="{y - 6}" width="{(hi - lo) * scale:.1f}" height="36" rx="8" fill="{col}" fill-opacity="0.35"/>')
        end = x0 + hi * scale
        if end > W - 150:
            b.append(text(end - 12, y + 18, label, 16, "#ffffff", 800, "end"))
        else:
            b.append(text(end + 12, y + 18, label, 16, PINK if name == "tinyreply" else INK, 800))
        if len(note) >= 24:
            b.append(text(446, y + 52, note, 12, MUTED))
        y += 76
    # moving parts
    b.append(text(410, 450, "Things to run and update", 18, INK, 700))
    parts = [("tinyreply", ["python"], "1 process"),
             ("OpenReply", ["nextdotjs", "nodedotjs", "postgresql", "redis"], "4+"),
             ("ChatbotX", ["docker", "postgresql", "redis", "nodedotjs"], "~10 services")]
    px = 410
    for name, slugs, label in parts:
        w = 246
        b.append(box(px, 466, w - 12, 74, SOFT, LINE, 14))
        b.append(text(px + 14, 490, name, 14, INK, 700))
        for k, s in enumerate(slugs):
            b.append(logo(s, px + 14 + k * 28, 504, 20, PINK if name == "tinyreply" else None))
        b.append(text(px + w - 26, 520, label, 14, PINK if name == "tinyreply" else MUTED, 700, "end"))
        px += w
    b.append(text(44, H - 22, "Others: their documentation, issue trackers and our reading of their stacks (Nov 2026); not benchmarks.", 13, FAINT))
    write("memory.svg", svg(W, H, "".join(b)))


# ---------------------------------------------------------------- report (English rendering of real output)
def report():
    # `bot.py report` run against the bundled fake Meta (sim/fake_meta.py) on 10 Nov 2026: 12 comments,
    # 11 matched, armed with 20, 3 delivered so far at the configured hourly pace. The bot prints this in
    # Russian; this is a line-by-line translation. Lines about the one-shot demo (`service not running`) omitted.
    lines = [
        ("h", "Report at 10.11 22:38 UTC"),
        ("", ""),
        ("h", "State:"),
        ("", "  sending: on"),
        ("", "  budget: 17 (armed 20 at 10.11 22:37 UTC)"),
        ("", "  account hold: none"),
        ("", "  send hold: none"),
        ("", "  public replies: on"),
        ("", "  account: @your_account (id 17841400000000001), checked 10.11 22:37 UTC"),
        ("", "  token: less than a minute old"),
        ("", "  Telegram: working (last delivery 10.11 22:37 UTC), queued 0"),
        ("g", "  unaccounted (journal vs comments): 0"),
        ("", ""),
        ("h", "Reels:"),
        ("", "  «arena» (17900000000000101): active, state ok, read 10.11 22:37 UTC"),
        ("", "    waiting for the link: 8 people, oldest 40 min, window closes in 6 d 23 h"),
        ("", "    why waiting: cap (the queue moves at the allowed pace)"),
        ("", "    done 3 · pending 8 · seen 1"),
        ("", ""),
        ("h", "DMs per hour, last 24 h (UTC):"),
        ("", "  10.11 22:00  attempts 3, delivered 3"),
        ("", ""),
        ("h", "All attempts by outcome:"),
        ("g", "  dm ok: 3"),
        ("g", "  public ok: 3"),
    ]
    W, lh = 1200, 22
    H = 108 + lh * len(lines) + 24
    b = [f'<rect width="{W}" height="{H}" rx="18" fill="#0f172a"/>',
         f'<rect width="{W}" height="52" rx="18" fill="#1e293b"/><rect y="34" width="{W}" height="18" fill="#1e293b"/>',
         '<circle cx="30" cy="26" r="7" fill="#ff5f57"/><circle cx="52" cy="26" r="7" fill="#febc2e"/>'
         '<circle cx="74" cy="26" r="7" fill="#28c840"/>',
         text(W / 2, 31, "igbot report", 14, "#cbd5e1", 600, "middle"),
         text(W - 24, 31, "translated from the Russian output · demo against the bundled fake Meta", 12, "#64748b", 400, "end"),
         text(28, 88, "$ igbot report", 15, "#f472b6", 400, font=MONO)]
    for i, (kind, s) in enumerate(lines):
        col = {"h": "#7dd3fc", "g": "#86efac"}.get(kind, "#e2e8f0")
        s = s.replace(" ", " ")
        b.append(text(28, 118 + i * lh, s, 15, col, 400, font=MONO))
    write("report.svg", svg(W, H, "".join(b)))


# ---------------------------------------------------------------- matcher
def matcher():
    # Results of match.Matcher(reel).check(text) for a reel with keywords ["guide"] and also_match ["gude"]
    # (otherwise config.example.json), printed by the code on 11 Nov 2026.
    rows = [("guide", "match", "the keyword"),
            ("GUIDE", "match", "case does not matter"),
            ("Guide!!", "match", "punctuation is ignored"),
            ("guiiide 🔥", "match", "repeated letters, emoji"),
            ("I want the guide", "match", "the keyword inside a short sentence"),
            ("@friend guide", "match", "@mentions are ignored"),
            ("guid\u0435", "match", "a look-alike Cyrillic “е” typed inside"),
            ("gude", "match", "a typo you listed in also_match"),
            ("guide?", "match", "a lone keyword with “?” is a request"),
            ("is the guide free?", "near", "a question: listed in the report, not sent"),
            ("send me the guide please I really need it", "near", "longer than max_words (5)"),
            ("guidance", "none", "a different word: not a match"),
            ("nice reel", "none", "no keyword")]
    W = 1200
    top, rh = 128, 44
    H = top + rh * len(rows) + 40
    b = [card(W, H)]
    b.append(text(44, 60, "What the keyword matcher does", 26, INK, 800))
    b.append(text(44, 90, "Keyword “guide”. Every row below is the actual output of match.py.", 16, MUTED))
    chip = {"match": (GREEN, GREEN_SOFT, "gets DM", "check"),
            "near": (AMBER, AMBER_SOFT, "flagged", "triangle-alert"),
            "none": (FAINT, SOFT, "ignored", "x")}
    for i, (t, kind, why) in enumerate(rows):
        y = top + i * rh
        if i % 2 == 0:
            b.append(f'<rect x="24" y="{y - 28}" width="{W - 48}" height="{rh}" rx="10" fill="{SOFT}"/>')
        b.append(text(48, y, t, 17, INK, 500, font=MONO))
        col, soft, label, ic = chip[kind]
        b.append(box(560, y - 21, 118, 30, soft, col, 15, 1.2))
        b.append(icon(ic, 572, y - 15, 18, col, 2.5))
        b.append(text(596, y - 1, label, 14, col, 700))
        b.append(text(700, y, why, 15, MUTED))
    b.append(text(44, H - 26, "Also tested on 1,108 real comments from a live reel (Russian and English spellings): every one matched.", 14, FAINT))
    write("matcher.svg", svg(W, H, "".join(b)))


# ---------------------------------------------------------------- failures
def failures():
    # SPEC.md 6.1 (classify), 7.3 (interrupted attempts), 8.5 (holds and their waits)
    cases = [
        ("clock", "Timeout or 5xx after sending", "Maybe delivered. Marked uncertain, never sent again. Sending pauses 15 min (up to 6 h) and you get an alert."),
        ("rotate-ccw", "Could not connect", "The request never left. The comment goes back to the queue and is tried in the next cycle."),
        ("check", "“Already replied” from Meta", "That person has their reply. Counted as served, no pause."),
        ("x", "Comment deleted, user unavailable, older than 7 days", "Closed with that reason. Everyone else continues."),
        ("zap", "Rate limit, revoked token, blocked app", "Sending pauses 30 min to 12 h, the account is re-checked, you get an alert. A new token in .env is picked up without a restart."),
        ("triangle-alert", "Flagged as spam", "24 h pause and the budget drops to 0. Nothing goes out until you arm again."),
        ("hard-drive", "Crash or power loss mid-send", "On restart the open attempt is closed as uncertain. It is never sent twice."),
        ("bell", "Five failures in a row", "Sending pauses 1 h (growing up to 12 h) instead of burning through the queue."),
    ]
    W, H = 1200, 676
    b = [card(W, H)]
    b.append(text(44, 60, "When something goes wrong", 26, INK, 800))
    b.append(text(44, 90, "Every answer from Meta is classified. Nothing is retried that could reach the same person twice.", 16, MUTED))
    cw, ch, gx, gy = 548, 120, 24, 18
    for i, (ic, title, desc) in enumerate(cases):
        col, row = i % 2, i // 2
        x, y = 44 + col * (cw + gx), 118 + row * (ch + gy)
        bad = ic in ("clock", "triangle-alert", "hard-drive")
        accent = RED if bad else (GREEN if ic == "check" else BLUE)
        b.append(box(x, y, cw, ch, SOFT, LINE, 16))
        b.append(f'<rect x="{x}" y="{y}" width="6" height="{ch}" rx="3" fill="{accent}"/>')
        b.append(f'<circle cx="{x + 44}" cy="{y + 40}" r="22" fill="#ffffff" stroke="{LINE}"/>')
        b.append(icon(ic, x + 32, y + 28, 24, accent))
        b.append(text(x + 80, y + 38, title, 16, INK, 700))
        words, line, out = desc.split(), "", []
        for w in words:
            if len(line) + len(w) > 56:
                out.append(line)
                line = w
            else:
                line = (line + " " + w).strip()
        out.append(line)
        for k, l in enumerate(out[:3]):
            b.append(text(x + 80, y + 64 + 20 * k, l, 14, MUTED))
    write("failures.svg", svg(W, H, "".join(b)))


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    for f in (hero, flow, architecture, footprint, report, matcher, failures):
        f()
    print("written:", ", ".join(sorted(os.listdir(OUT))))
