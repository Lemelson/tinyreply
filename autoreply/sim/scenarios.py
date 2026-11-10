"""Scenarios for sim/soak.py (SPEC.md section 11): build(seed) for a random one, named(name) for a fixed one.

A scenario is plain JSON:
    name, seed, scale      label, the seed it came from, the clock speed-up it was written for
    fake                   the scenario of sim/fake_meta.py (sim/README.md)
    config, env            the bot's config.json and .env (env None = no file)
    steps                  the schedule the soak runs, in order (see STEPS)
    settle                 true: afterwards end all faults, open the gates and wait for the bot to finish
    settle_timeout         scaled seconds the bot gets for that
    faults                 the referee's catalogue {fault id: {"klass": ...}}: what an injected answer MEANS,
                           written by hand from SPEC 6.1 (ok | account | spam | item | ambiguous)
    may_expire             comments the schedule itself may leave unanswered until their window closes
    expect                 {"waiting": reason} when the run ends with a gate closed on purpose

STEPS (every step is {"op": ..., ...}; any step may carry "alert": {"contains", "within", "count"}):
    start | term | second            spawn `bot.py run` | SIGTERM (must exit 0) | a second runner (must exit 3)
    kill kind phase [target] [vanish] [arm]  SIGKILL while the fake holds that request `before` or `after`
                                     processing; vanish: the held request is never processed; arm: arm that
                                     much once the hold is in place, so that the DM cannot slip out before it
    arm n [deaf] [rc] | disarm       n: a number, "all", or "waiting" = everybody who waits at that moment
                                     plus 2 (`arm` SETS the budget, it does not add)
    mode mode | faults add | comment add/delete/hide/owner_reply | env env
    config config relabel            restart with this other config, which disarms (SPEC 4); relabel =
                                     should_match from then on
    wait sec | until what [...] [timeout]   scaled seconds | rows, dm (count since the last arm), status (ids,
                                     among), ok (kind), fired (fault ids)
                                     until/kill/fault are required by default. Optional requires required=false
                                     and a reason. Fault min_fires defaults to 1 (Plan uses every explicit nth).
    settle                           require all installed faults, clear them, open gates, require progress
    quiet                            up to now the fake must not have heard from the bot (Telegram aside)

A scenario must be FAIR: a bot that follows SPEC.md passes every invariant in it. The rules that keep random
mixes fair are stated where they are applied.
"""
import copy
import itertools
import random

DAY, HOUR, MINUTE = 86400, 3600, 60
ACCOUNT = {"user_id": "17841400000000001", "id": "9990000000000001", "username": "your_account",
           "followers_count": 1234, "media_count": 7}
TOKEN = "IGAAsoakTOKENx1"
TELEGRAM = {"TG_BOT_TOKEN": "424242:soakTGsecret", "TG_CHAT_ID": "42"}
ENV = {"IG_ACCESS_TOKEN": TOKEN, **TELEGRAM}
MEDIA = ("18000000000000001", "18000000000000002")
EXTRA_WORD = "орена"    # a typo that is a request only while the config lists it in also_match
TERMINAL = ["done", "uncertain", "refused", "expired", "dup_user", "already"]

# Labels by SPEC section 5, written by hand (sim/test_invariants.py compares them with match.py).
REQUESTS = ("арена", "Арена", "арену", "ARENA", "arena", "хочу арену", "арена пожалуйста 🔥", "арена?",
            "Aрена", "аренааа", "@your_account арена", "арееа")
NEAR = ("зачем арена?", "как получить арену", "арена это что", "хочу арену?",
        "а арена реально работает или нет вообще")
OTHER = ("круто", "🔥🔥🔥", "Арина", "Ирена", "Алёна", "спасибо!", "к чему это", "", "класс 👍")
OWN = "Пиши арена в комментах"

# What the fake is told to answer, and what that answer means (SPEC 6.1, by hand).
AMBIGUOUS = ({"http": 500, "code": 2, "message": "An unexpected error has occurred."},
             {"http": 400, "code": 1, "message": "An unknown error occurred"},
             {"http": 400, "code": -1, "message": "Unknown"},
             {"http": 503, "body": "<html><body>503 Service Unavailable</body></html>"},
             {"omit_id": True},
             {"drop": "after"})
ITEM = ({"code": 551, "message": "This person isn't available right now."},
        {"code": 100, "subcode": 2534014, "message": "The requested user cannot be found."},
        {"code": 10, "subcode": 2018108, "message": "This person can't receive messages."},
        {"code": 100, "subcode": 2018001, "message": "No matching user found"},
        {"code": 10, "subcode": 2534022, "message": "This message is sent outside of allowed window."},
        {"code": 100, "subcode": 2534015, "message": "Invalid message data", "consume": True})
ACCOUNT_WIDE = ({"code": 190, "message": "Invalid OAuth access token"},
                {"code": 4, "message": "Application request limit reached"},
                {"code": 613, "message": "Calls to this api have exceeded the rate limit."},
                {"http": 429, "body": "Too Many Requests"},
                {"code": 10, "message": "Application does not have permission for this action"})
SPAM = {"code": 368, "message": "The action attempted has been deemed abusive or is otherwise disallowed"}
PUBLIC_GONE = {"code": 100, "subcode": 33, "message": "Unsupported post request."}


def reel(name, media_id, link):
    return {"name": name, "media_id": media_id, "permalink": "", "active": True, "keywords": ["арена", "arena"],
            "also_match": ["арееа"], "max_words": 5, "link": link,
            "text": "{greeting} Держи ссылку 👇 Кинь ее Клоду."}


def config(**changes):
    # digest_hour_utc is set by the soak to the hour after the run begins, so that every run reaches the digest
    return {"api_version": "v26.0", "sending": True, "poll_interval_sec": 30, "max_sends_per_hour": 120,
            "public_reply": True, "profile_url": "https://www.instagram.com/your_account/",
            "greeting": "Привет! 👋", "late_greeting": "Привет! 👋 Лучше поздно, чем никогда 😄",
            "late_after_minutes": 60,
            "public_phrases": ["Отправил в директ 📩", "Лови в личке 🔥", "Готово, проверь сообщения ✌️"],
            "reels": [reel("arena", MEDIA[0], "https://github.com/example/arena-skill"),
                      reel("second", MEDIA[1], "https://github.com/example/second-skill")],
            "alerts": {"repeat_min": 120, "digest_hour_utc": 0}, **changes}


class Plan:
    """A scenario under construction: comments, faults and the schedule, with unique ids."""

    def __init__(self, name, seed=0, scale=2000):
        self.name, self.seed, self.scale = name, seed, scale
        self.rng = random.Random(seed)
        self.config, self.env = config(), dict(ENV)
        self.initial = {media_id: [] for media_id in MEDIA}
        self.fake = {"limits": {"page_size": 50}, "already_replied": {"spelling": "10900", "deliver": False}}
        self.steps, self.faults, self.may_expire, self.expect = [], {}, [], {}
        self.settle, self.settle_timeout = True, 4 * HOUR
        self.deaf = False      # Telegram may have failed: `arm` needs --deaf from here on
        self._numbers = itertools.count(1)

    def user(self):
        number = next(self._numbers)
        return {"id": f"6{number:014d}", "username": f"user_{number}"}

    def comment(self, text="арена", who="new", should_match=True, age=10 * MINUTE, **more):
        """A comment for the fake. `who`: "new" person, None (Meta shows no author), "own", or a user."""
        author = self.user() if who == "new" else dict(id=ACCOUNT["id"], username=ACCOUNT["username"]) \
            if who == "own" else who
        return {"id": f"17{next(self._numbers):015d}", "text": text, "age_sec": age, "from": author,
                "should_match": should_match, **more}

    def load(self, spec, media=MEDIA[0]):
        """Present when the bot first looks."""
        self.initial[media].append(spec)
        return spec["id"]

    def add(self, spec, media=MEDIA[0]):
        """Written while the bot runs."""
        self.step("comment", add={"media_id": media, **spec})
        return spec["id"]

    def fresh(self, count, text=None, **more):
        """That many new people ask now; returns their comment ids."""
        return [self.add(self.comment(text or self.rng.choice(REQUESTS), age=0, **more)) for _ in range(count)]

    def step(self, op, **fields):
        self.steps.append({"op": op, **fields})

    def fault(self, klass, kind, answer, *, required=True, reason="", min_fires=None, **spec):
        """A fault for the fake. `klass` says what the answer means and goes into the referee's catalogue
        (None: the fault does not replace the answer of a POST). A replaced answer delivers nothing unless
        `deliver` says so."""
        fault_id = f"{klass or 'fault'}{next(self._numbers)}"
        if klass:
            self.faults[fault_id] = {"klass": klass}
        if answer and "drop" not in answer and kind in ("dm", "public", "telegram"):
            spec.setdefault("deliver", False)
        repeats = len(spec["nth"]) if isinstance(spec.get("nth"), list) else 1
        self.step("faults", add=[{"id": fault_id, "kind": kind, **answer, **spec}],
                  required=required, reason=reason, min_fires=repeats if min_fires is None else min_fires)
        return fault_id

    def arm(self, n="waiting", **fields):
        self.step("arm", n=n, **({"deaf": True} if self.deaf else {}), **fields)

    def until(self, what, timeout=2 * HOUR, **fields):
        self.step("until", what=what, timeout=timeout, **fields)

    def scenario(self):
        fake = {"account": dict(ACCOUNT), "token": {"value": TOKEN, "age_sec": 2 * DAY},
                "telegram": {"token": TELEGRAM["TG_BOT_TOKEN"], "chat_id": TELEGRAM["TG_CHAT_ID"]},
                "media": [{"id": media_id, "permalink": f"https://www.instagram.com/reel/DSoak{index}/",
                           "shortcode": f"DSoak{index}", "age_sec": 20 * DAY, "comments": comments}
                          for index, (media_id, comments) in enumerate(self.initial.items())], **self.fake}
        return copy.deepcopy({
            "name": self.name, "seed": self.seed, "scale": self.scale, "fake": fake, "config": self.config,
            "env": self.env, "steps": self.steps, "settle": self.settle, "settle_timeout": self.settle_timeout,
            "faults": self.faults, "may_expire": self.may_expire, "expect": self.expect})


# ---------- random scenarios ----------

def populate(plan):
    """The comments that are there when the bot starts: people who ask (some more than once), near-misses,
    chatter, the account's own words, comments Meta shows oddly, comments from before the bot."""
    rng = plan.rng
    for _ in range(rng.randint(5, 12)):
        person = plan.user()
        media = rng.choice(MEDIA) if rng.random() < 0.3 else MEDIA[0]
        plan.load(plan.comment(rng.choice(REQUESTS), person, age=rng.randint(MINUTE, 3 * DAY)), media)
        if rng.random() < 0.35:
            for _ in range(rng.randint(1, 2)):
                again = rng.random() < 0.6
                text = rng.choice(REQUESTS) if again else rng.choice(NEAR + OTHER)
                plan.load(plan.comment(text, person, should_match=again, age=rng.randint(MINUTE, 3 * DAY)), media)
    for text in rng.sample(NEAR, 2) + rng.sample(OTHER, 3):
        plan.load(plan.comment(text, should_match=False, age=rng.randint(MINUTE, 5 * DAY)), rng.choice(MEDIA))
    plan.load(plan.comment(OWN, "own", age=rng.randint(HOUR, 5 * DAY)))
    odd = [dict(who=None), dict(raw={"timestamp": None}), dict(age=7 * DAY + rng.randint(HOUR, DAY)),
           dict(replies=[{"text": "Отправил!", "from": dict(id=ACCOUNT["id"], username=ACCOUNT["username"])}]),
           dict(listed=False), dict(private_replied=True)]
    for more in rng.sample(odd, rng.randint(2, len(odd))):
        plan.load(plan.comment(rng.choice(REQUESTS), **more))
    plan.fake["limits"]["page_size"] = rng.randint(3, 8)    # sweeps of several pages
    plan.fake["already_replied"]["spelling"] = rng.choice(["10900", "2534025", "-1"])


def stages(plan):
    """Arm a little, see it used up, disarm, more people, arm again."""
    first = plan.rng.randint(1, 3)
    plan.fresh(first)
    plan.arm(first)
    plan.until("dm", count=first, timeout=30 * MINUTE)
    if plan.rng.random() < 0.5:
        plan.step("disarm")
        plan.step("wait", sec=plan.rng.randint(5, 15) * MINUTE)
    more = plan.rng.randint(1, 3)
    plan.fresh(more)
    plan.arm()
    plan.until("dm", count=more, timeout=30 * MINUTE)


def doubt(plan):
    """Answers that leave the DM in doubt, delivered or not."""
    victims = plan.fresh(plan.rng.randint(1, 2))
    for victim in victims:
        answer = plan.rng.choice(AMBIGUOUS + ({"drop": "before"},))
        more = {} if answer.get("drop") == "before" else {"deliver": plan.rng.random() < 0.5}
        plan.fault("ambiguous", "dm", answer, target=victim, nth=1, **more)
    plan.arm()
    plan.until("status", ids=victims, among=TERMINAL)


def rejections(plan):
    """Meta refuses single people; one comment is deleted, one hidden, one answered by the owner by hand
    before the bot gets to them."""
    victims = plan.fresh(plan.rng.randint(1, 2))
    for victim in victims:
        plan.fault("item", "dm", plan.rng.choice(ITEM), target=victim, nth=1)
    gone, hidden, answered = plan.fresh(3)
    plan.until("rows", timeout=HOUR)
    plan.step("comment", delete=gone)
    plan.step("comment", hide=hidden)
    plan.step("comment", owner_reply=answered)
    plan.arm()
    plan.until("status", ids=victims + [gone, hidden, answered], among=TERMINAL)


def account_wide(plan):
    """A definite account-wide refusal of one POST: the person stays in the queue and is asked again later."""
    victim, = plan.fresh(1)
    plan.fresh(plan.rng.randint(1, 2))
    plan.fault("account", "dm", plan.rng.choice(ACCOUNT_WIDE), target=victim, nth=plan.rng.choice([1, [1, 2]]))
    plan.arm()
    plan.until("status", ids=[victim], among=TERMINAL, timeout=3 * HOUR)


def public_trouble(plan):
    """The DM arrives, the public reply does not, or nobody knows."""
    victims = plan.fresh(plan.rng.randint(1, 3))
    for victim in victims:
        klass, answer = plan.rng.choice([("ambiguous", AMBIGUOUS[0]), ("ambiguous", AMBIGUOUS[5]),
                                         ("item", PUBLIC_GONE), ("account", ACCOUNT_WIDE[0])])
        more = {"deliver": plan.rng.random() < 0.5} if klass == "ambiguous" else {}
        plan.fault(klass, "public", answer, target=victim, nth=1, **more)
    plan.arm()
    plan.until("status", ids=victims, among=TERMINAL)


def outage(plan):
    """The account is blocked, the token dies or Meta throttles; later it is over and the bot must notice both."""
    plan.until("ok", kind="comments", timeout=HOUR)    # no hold left over: the alert below must be a new one
    plan.step("mode", mode=plan.rng.choice(["blocked", "token_dead", "throttled"]),
              alert={"contains": ["hold:account"], "within": 20 * MINUTE})
    plan.fresh(plan.rng.randint(1, 2))
    plan.step("wait", sec=plan.rng.randint(30, 60) * MINUTE)
    plan.step("mode", mode="ok")
    plan.until("ok", kind="comments", timeout=45 * MINUTE)    # the probe lifted the hold: sweeps are back
    plan.arm()


def partial_block(plan):
    """Messaging alone is blocked, or the app sees empty comment lists; then it ends."""
    plan.step("mode", mode=plan.rng.choice(["messaging_blocked", "dev_mode"]))
    plan.fresh(plan.rng.randint(1, 2))
    plan.arm()
    plan.step("wait", sec=plan.rng.randint(35, 70) * MINUTE)
    plan.step("mode", mode="ok")


def bad_line(plan):
    """Required refused connections, slow DM, two read failures, media failure and Telegram failure."""
    plan.deaf = True
    rng = plan.rng
    faults = [plan.fault(None, "comments", {}, nth=1, refuse_real_sec=round(rng.uniform(0.2, 0.5), 2)),
              plan.fault(None, "comments", {"http": 500, "code": 2, "message": "Unexpected error"}, nth=[2, 3]),
              plan.fault(None, "media", {"code": 1, "message": "Unknown error"}, nth=1),
              plan.fault(None, "dm", {}, nth=1, delay_real_sec=round(rng.uniform(0.02, 0.1), 2)),
              plan.fault(None, "telegram", {"http": 500, "message": "Internal Server Error"}, nth=1)]
    plan.fresh(3)
    plan.arm()
    plan.until("fired", ids=faults, timeout=3 * HOUR)


def crashes(plan):
    """kill -9 at the points the fake can hold, SIGTERM, a second runner: the bot is started again each time."""
    rng = plan.rng
    for point in rng.sample(["dm_before", "dm_lost", "dm_after", "public", "sweep", "term", "second"],
                            rng.randint(2, 4)):
        if point.startswith("dm"):
            victim, = plan.fresh(1)
            plan.step("kill", kind="dm", phase="after" if point == "dm_after" else "before", target=victim,
                      vanish=point == "dm_lost", arm="waiting", deaf=plan.deaf)
        elif point == "public":    # between the DM and its public reply
            victim, = plan.fresh(1)
            plan.step("kill", kind="public", phase="before", target=victim, vanish=True, arm="waiting",
                      deaf=plan.deaf)
        elif point == "sweep":
            plan.step("kill", kind="comments", phase=rng.choice(["before", "after"]))
        else:
            plan.step(point)
        if point != "second":
            plan.step("start")


def reconfigure(plan):
    """A restart with another config: a typo stops or starts being a request, or the message changes."""
    rng = plan.rng
    new = copy.deepcopy(plan.config)
    kind = rng.choice(["remove_word", "add_word", "new_message"])
    if kind == "remove_word":
        for one in plan.config["reels"]:    # the word was in the config from the start
            one["also_match"] = one["also_match"] + [EXTRA_WORD]
        typos = plan.fresh(2, text=EXTRA_WORD)
        plan.until("rows", timeout=HOUR)
        plan.step("config", config=new, relabel=dict.fromkeys(typos, False))
    elif kind == "add_word":
        typos = [plan.add(plan.comment(EXTRA_WORD, should_match=False, age=0)) for _ in range(2)]
        plan.until("rows", timeout=HOUR)
        for one in new["reels"]:
            one["also_match"] = one["also_match"] + [EXTRA_WORD]
        plan.step("config", config=new, relabel=dict.fromkeys(typos, True))
    else:
        new.update(greeting="Хей! ✌️", late_greeting="Хей! ✌️ Прости, что так долго",
                   max_sends_per_hour=rng.choice([60, 300]), public_reply=rng.random() < 0.8)
        plan.step("config", config=new, relabel={})
        plan.fresh(2)
    plan.arm()


def spam(plan):
    """Meta calls one DM abusive: a day of silence, and a human has to arm again."""
    victim, = plan.fresh(1)
    plan.fresh(1)
    fault = plan.fault("spam", "dm", SPAM, target=victim, nth=1)
    plan.arm()
    plan.until("fired", ids=[fault])
    plan.scale = 6000
    plan.settle_timeout += 23 * HOUR    # the day of silence comes on top of what the other blocks need


BLOCKS = (stages, doubt, rejections, account_wide, public_trouble, outage, partial_block, bad_line, crashes,
          reconfigure)


def build(seed):
    """The random scenario of a seed: the same seed gives the same scenario, kill points included."""
    plan = Plan(f"seed-{seed}", seed)
    rng = plan.rng
    blocks = rng.sample(BLOCKS, rng.randint(2, 3))
    if rng.random() < 0.08:
        blocks.append(spam)
    populate(plan)
    plan.fake["already_replied"]["deliver"] = rng.random() < 0.5
    # SPEC 9, `unservable`: a request Meta lists without an author or without a timestamp cannot be answered,
    # and that is said (I12)
    odd = any(comment["should_match"] and (comment["from"] is None or "timestamp" in comment.get("raw", {}))
              for comments in plan.initial.values() for comment in comments)
    plan.step("start", **({"alert": {"contains": ["unservable"], "within": HOUR}} if odd else {}))
    plan.until("rows", timeout=HOUR)
    for block in blocks:
        # Stop fresh claims before creating targets; install faults before the next arm.
        plan.step("disarm")
        block(plan)
        # Each selected experiment must complete before another experiment changes its gates.
        plan.step("settle")
    return plan.scenario()


# ---------- named scenarios: one per guarantee, one per historical outage, one per mechanism ----------

def g1_one_dm_per_person():
    """G1: people who ask three times, under two reels, through a crash that leaves one DM in doubt."""
    plan = Plan("g1")
    people = [plan.user() for _ in range(4)]
    firsts = [plan.load(plan.comment("арена", person, age=3 * HOUR)) for person in people]
    for person in people:
        plan.load(plan.comment("Арена", person, age=2 * HOUR))
        plan.load(plan.comment("arena", person, age=HOUR), MEDIA[1])
    plan.step("start")
    plan.until("rows")
    plan.step("kill", kind="dm", phase="after", target=firsts[1], arm="all")
    plan.step("start")
    for person in people:
        plan.add(plan.comment("арену", person, age=0))
    return plan.scenario()


def g2_rate():
    """G2: thirty people at once, 120 per hour, a crash in the middle: never closer than a slot."""
    plan = Plan("g2")
    for index in range(30):
        plan.load(plan.comment(REQUESTS[index % len(REQUESTS)], age=(40 - index) * MINUTE))
    plan.step("start")
    plan.until("rows")
    plan.step("arm", n="all")
    plan.until("dm", count=8, timeout=HOUR)
    plan.step("kill", kind="dm", phase="before")
    plan.step("start")
    plan.until("dm", count=8, timeout=HOUR)
    plan.step("term")
    plan.step("start")
    return plan.scenario()


def g3_only_requests():
    """G3: the account's own words, near-misses, chatter, no author; a typo dropped from the config while
    its writers still wait."""
    plan = Plan("g3")
    for one in plan.config["reels"]:
        one["also_match"] = one["also_match"] + [EXTRA_WORD]
    for text in REQUESTS:
        plan.load(plan.comment(text))
    for text in NEAR + OTHER:
        plan.load(plan.comment(text, should_match=False))
    plan.load(plan.comment(OWN, "own"))
    plan.load(plan.comment("арена", {"id": ACCOUNT["user_id"], "username": "renamed"}))
    plan.load(plan.comment("арена", None))
    typos = [plan.load(plan.comment(EXTRA_WORD)) for _ in range(3)]
    plan.step("start")
    plan.until("rows")
    stricter = config()
    plan.step("config", config=stricter, relabel=dict.fromkeys(typos, False))
    plan.step("arm", n="all")
    return plan.scenario()


def g4_never_again():
    """G4: every ambiguous answer with and without delivery, every per-item refusal, the three spellings of
    "already replied" for a comment answered before the bot existed. Nobody is asked twice."""
    plan = Plan("g4")
    plan.config["max_sends_per_hour"] = 300
    age, faults = 3 * HOUR, []
    for index, answer in enumerate(AMBIGUOUS + ({"drop": "before"},) + ITEM):
        for deliver in (True, False) if index < len(AMBIGUOUS) else (False,):
            victim = plan.load(plan.comment(age=age))
            more = {} if answer.get("drop") == "before" else {"deliver": deliver}
            faults.append(plan.fault("ambiguous" if index <= len(AMBIGUOUS) else "item", "dm", answer,
                                     target=victim, nth=1, **more))
            # somebody who gets through after each of them: the holds stay at their first step
            plan.load(plan.comment(age=age - MINUTE))
            age -= 2 * MINUTE
    plan.load(plan.comment(private_replied=True))
    plan.fake["already_replied"] = {"spelling": "-1", "deliver": True}
    plan.step("start")
    plan.until("rows")
    plan.step("arm", n="all")
    plan.until("fired", ids=faults, timeout=8 * HOUR)    # thirteen doubts, a quarter of an hour of hold after each
    return plan.scenario()


def g5_everything_accounted():
    """G5: 120 comments over many pages, some arriving with old dates, one deleted, one past the window."""
    plan = Plan("g5")
    plan.fake["limits"]["page_size"] = 7
    plan.config["max_sends_per_hour"] = 300
    for index in range(120):
        request = index % 3 == 0
        plan.load(plan.comment(REQUESTS[index % len(REQUESTS)] if request else OTHER[index % len(OTHER)],
                               should_match=request, age=index * 7 * MINUTE + MINUTE))
    plan.load(plan.comment(age=8 * DAY))
    plan.load(plan.comment(raw={"timestamp": None}))
    plan.step("start")
    plan.until("rows")
    deleted = plan.add(plan.comment(age=4 * DAY))    # appears deep in the list: only a full sweep finds it
    plan.add(plan.comment(age=5 * DAY))
    plan.until("rows", timeout=HOUR)
    plan.step("comment", delete=deleted)
    plan.step("arm", n="all")
    return plan.scenario()


def g6_gates():
    """G6: a fresh database sends nothing; arm 2 sends two; disarm stops; a config with sending off sends
    nothing however much is armed."""
    plan = Plan("g6")
    plan.config["max_sends_per_hour"] = 30    # slots of two minutes: `disarm` is in time to leave people waiting
    for index in range(12):
        plan.load(plan.comment(age=(20 - index) * MINUTE))
    plan.step("start")
    plan.until("rows")
    plan.step("wait", sec=20 * MINUTE)
    plan.step("arm", n=2)
    plan.until("dm", count=2, timeout=30 * MINUTE)
    plan.step("wait", sec=20 * MINUTE)
    plan.step("arm", n=5)
    plan.until("dm", count=1, timeout=30 * MINUTE)
    plan.step("disarm")
    plan.step("wait", sec=20 * MINUTE)
    plan.step("config", config=config(sending=False, max_sends_per_hour=30), relabel={})
    plan.step("arm", n="all", alert={"contains": ["waiting", "sending_off"], "within": 50 * MINUTE})
    plan.step("wait", sec=HOUR)
    plan.settle, plan.expect = False, {"waiting": "sending_off"}
    return plan.scenario()


def outage_blocked():
    """Outage 1: the API is blocked for hours while people's windows run out. Nobody is written off as
    refused, the hold is announced again and again, the lost are named, and it recovers by itself."""
    plan = Plan("outage_blocked", scale=4000)
    for index in range(4):
        plan.may_expire.append(plan.load(plan.comment(age=7 * DAY - (2 + index) * HOUR)))
    for index in range(6):
        plan.load(plan.comment(age=(index + 1) * HOUR))
    plan.step("start")
    plan.until("rows")
    plan.step("mode", mode="blocked", alert={"contains": ["hold:account"], "within": 7 * HOUR, "count": 3})
    plan.step("arm", n="all", deaf=True)
    plan.step("wait", sec=7 * HOUR)
    plan.step("mode", mode="ok")
    return plan.scenario()


def outage_gate():
    """Outage 2: a gate is left closed (never armed) with people waiting and expiring. Nothing is sent, the
    wait is announced and repeated, and the bot says why."""
    plan = Plan("outage_gate", scale=4000)
    for index in range(3):
        plan.may_expire.append(plan.load(plan.comment(age=7 * DAY - (2 + index) * HOUR)))
    for index in range(5):
        plan.load(plan.comment(age=(index + 1) * HOUR))
    plan.step("start", alert={"contains": ["waiting", "no_budget"], "within": 6 * HOUR, "count": 2})
    plan.step("wait", sec=6 * HOUR)
    plan.settle, plan.expect = False, {"waiting": "no_budget"}
    return plan.scenario()


def outage_secret():
    """Outage 3: the secret is missing. No call to Meta, `arm` refuses, Telegram hears of it as soon as it
    can; once the token is there everybody is served."""
    plan = Plan("outage_secret")
    for index in range(5):
        plan.load(plan.comment(age=(index + 1) * 10 * MINUTE))
    plan.env = None
    plan.step("start")
    plan.step("wait", sec=20 * MINUTE)
    plan.step("arm", n=5, rc=1)
    plan.step("env", env=dict(TELEGRAM), alert={"contains": ["hold:account", "no_token"], "within": 20 * MINUTE})
    plan.step("wait", sec=20 * MINUTE)
    plan.step("quiet")
    plan.step("env", env=dict(ENV))
    plan.until("ok", kind="comments", timeout=HOUR)
    return plan.scenario()


def kill_points():
    """Every place a kill can be put, SIGTERM and a second runner, one after another."""
    plan = Plan("kill_points")
    for index in range(6):
        plan.load(plan.comment(age=(index + 1) * MINUTE))
    plan.step("start")
    plan.until("rows")
    plan.step("second")
    for phase, vanish in (("before", False), ("before", True), ("after", False)):
        plan.step("kill", kind="dm", phase=phase, vanish=vanish, arm=1)
        plan.step("start")
    plan.step("kill", kind="public", phase="before", vanish=True, arm=1)
    plan.step("start")
    for phase in ("before", "after"):
        plan.step("kill", kind="comments", phase=phase)
        plan.step("start")
    plan.step("term")
    plan.step("start")
    plan.settle_timeout = 12 * HOUR
    return plan.scenario()


def account_refusal_preserved():
    """Repeated account refusal never becomes per-item; after faults end the person is served."""
    plan = Plan("account_refusal_preserved")
    victim = plan.load(plan.comment(age=HOUR))
    for index in range(4):
        plan.load(plan.comment(age=(index + 1) * MINUTE))
    fault = plan.fault("account", "dm", ACCOUNT_WIDE[4], target=victim, nth=[1, 2], min_fires=2)
    plan.step("start")
    plan.until("rows")
    plan.step("arm", n="all")
    plan.until("fired", ids=[fault], timeout=6 * HOUR)
    plan.until("status", ids=[victim], among=["done"], timeout=6 * HOUR)
    return plan.scenario()


def messaging_blocked():
    """Reads work, every DM is refused: the bot must back off further each time, not ask every 5 minutes."""
    plan = Plan("messaging_blocked", scale=4000)
    for index in range(5):
        plan.load(plan.comment(age=(index + 1) * MINUTE))
    plan.step("start")
    plan.until("rows")
    plan.step("mode", mode="messaging_blocked", alert={"contains": ["hold:send"], "within": 20 * MINUTE})
    plan.step("arm", n="all")
    plan.step("wait", sec=5 * HOUR)
    plan.step("mode", mode="ok")
    plan.settle_timeout = 9 * HOUR
    return plan.scenario()


def config_changed():
    """A config edit across a restart turns old comments into requests: nothing goes out until a human arms."""
    plan = Plan("config_changed")
    typos = [plan.load(plan.comment(EXTRA_WORD, should_match=False, age=(index + 1) * MINUTE)) for index in range(5)]
    plan.step("start")
    plan.until("rows")
    plan.step("arm", n="all")
    wider = config()
    for one in wider["reels"]:
        one["also_match"] = one["also_match"] + [EXTRA_WORD]
    plan.step("config", config=wider, relabel=dict.fromkeys(typos, True),
              alert={"contains": ["config_changed"], "within": 20 * MINUTE})
    plan.step("wait", sec=30 * MINUTE)
    return plan.scenario()


def spam_block():
    """A DM answered "abusive": 24 hours without any POST and no sending before a new arm."""
    plan = Plan("spam")
    for index in range(4):
        plan.load(plan.comment(age=(index + 1) * MINUTE))
    plan.step("start")
    plan.until("rows")
    spam(plan)
    plan.steps[-2]["alert"] = {"contains": ["hold:send"], "within": 20 * MINUTE}    # on the arm
    return plan.scenario()


NAMED = {"g1": g1_one_dm_per_person, "g2": g2_rate, "g3": g3_only_requests, "g4": g4_never_again,
         "g5": g5_everything_accounted, "g6": g6_gates, "outage_blocked": outage_blocked,
         "outage_gate": outage_gate, "outage_secret": outage_secret, "kill_points": kill_points,
         "account_refusal_preserved": account_refusal_preserved, "messaging_blocked": messaging_blocked,
         "config_changed": config_changed, "spam": spam_block}


def named(name):
    return NAMED[name]()
