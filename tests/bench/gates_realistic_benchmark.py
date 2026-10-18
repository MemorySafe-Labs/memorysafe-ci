"""Realistic assistant-memory benchmark for the opt-in gates. DESIGN EVIDENCE ONLY.

A synthetic, seeded workload that imitates what an AI assistant stores about one user and
their projects over `--weeks` simulated weeks, written as plain-language memories, run
through the real plugin storage (`MemoryStore.remember` / `find`) with a simulated clock.

What is in the stream (all templates are generic and synthetic; no real user data):
  preferences     stable likes/dislikes; some are mentioned again later in other words
  project facts   who/where/which facts about 4 projects; some mentioned again
  evolving facts  job title, employer, city, street address, two project deadlines, issue
                  tracker, database, stand-up time, budget, editor, manager -- 2-3 versions
                  each, weeks apart, some corrections worded so the shipped slot check can
                  see the change and some not; some current values restated later
  critical items  allergies, a safety constraint, legal deadlines, decisions with reasons:
                  mentioned ONCE early and only asked about 8-14 weeks later
  throwaways      one-off remarks (some look like facts), small talk, routine task-log
                  lines (some numbered); never asked about. About three quarters of all writes.
  decoys          a few throwaways that share words with a critical item
  recall tasks    queries at set days with ground-truth markers (see below), asked through
                  `find(query, 3)` so recall counts accrue as in real use

Capacity (`--capacity`, default 200) is far below the number of writes, so every arm must
choose what to drop. Arms (retrieval is identical, only retention differs):
  fifo            oldest active memory out, protected or not (policy run by this script)
  shipped         MEMORYSAFE_MAX_ACTIVE (the shipped eviction order)
  gate_cap        MEMORYSAFE_GATE_CAPACITY
  gate_cap+sur    MEMORYSAFE_GATE_CAPACITY + MEMORYSAFE_GATE_SURPRISE
  unbounded       no capacity at all (information only: the retrieval ceiling)

Category labels are what an assistant would plausibly pick and are NOT clean: e.g. an
allergy is filed as "safety" only 55% of the time (shipped protects "safety"/"decision"
automatically, and anything with importance >= 0.8, e.g. "preference" and "personal").
`--labels clean` files every critical item under safety/decision and every throwaway under
other/task, to show how much each arm leans on the label.

Ground truth: every fact has marker strings (lower-case substrings). A query HITS when one
of the top 3 results contains a marker of the correct answer. For an evolving fact the
correct answer is the version current on the day of the query; a query is a "change query"
once at least one correction has been stated. LATEST-FACT CORRECT = top result carries the
current value. STALE LEAK = top result carries an older value and not the current one.

Metrics (per seed, then mean over seeds): recall hit by type (preference, project, evolving,
critical, and facts that were mentioned more than once), rare-critical survival at the end
(any active memory carrying the marker), latest-fact correct, stale leak, wrong merges
(a write folded into a memory that holds a different fact or a different version of the
fact), throwaways retained (share of throwaway writes whose memory is still active) and
their share of the final store, write latency mean / p95 (ms, remember() only).

Verdict per arm vs shipped (the owner's rule: product direction only if faster AND smarter):
  smarter  = at least one quality metric better than shipped by >= 0.02 (0.5 for wrong
             merges) and by more than 2 standard errors of the paired per-seed difference,
             and none worse by the same test (throwaways retained counts as a quality metric)
  faster   = mean write ms lower than shipped by more than 2 standard errors (paired)
  "faster+smarter" | "smarter only" | "worse" (more metrics worse than better) | "exploratory"
Verdicts are about this synthetic stream; they are not evidence about real memories.

Seeds 0..7 are the reported seeds. Seeds 100..104 (--tune) are the only seeds used while
building this script and while setting any gate constant.

Run: PYTHONPATH=src python tests/bench/gates_realistic_benchmark.py [--seeds 8] [--tune] [--json out.json]
Temp dirs only; never touches a live store or port 8765.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from memorysafe_chatgpt import gates, storage  # noqa: E402
from memorysafe_chatgpt.storage import MemoryStore  # noqa: E402

START = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------------------
# Content pools. Every list is generic and synthetic.
# ---------------------------------------------------------------------------------------

USERS = ["Maya", "Jonas", "Priya", "Tomas", "Aiko", "Lena", "Omar", "Sofia"]

# (variants, query, markers). {u} = the user's first name.
PREFERENCES = [
    (["{u} prefers answers as short bullet points rather than long paragraphs",
      "Keep replies to {u} in bullet form because long paragraphs annoy {u}",
      "{u} likes summaries as bullets, not prose"],
     "what format does {u} want for answers and summaries", ("bullet",)),
    (["{u} drinks an oat milk flat white with no sugar",
      "For coffee {u} always orders a flat white with oat milk",
      "{u}'s usual coffee is a flat white, oat milk"],
     "what is {u}'s usual coffee order", ("flat white",)),
    (["{u} prefers a window seat on flights and books it early",
      "Always book {u} a window seat when flying",
      "{u} hates the middle seat so the window seat is the default"],
     "which seat does {u} want on flights", ("window seat",)),
    (["{u} wants all measurements in metric units",
      "Use metric units (km, kg, celsius) when talking with {u}",
      "{u} thinks in celsius and kilometres, so skip imperial"],
     "which units should I use with {u}", ("metric", "celsius")),
    (["{u} does not like phone calls, email or chat first",
      "Contact {u} by email or chat before ever calling",
      "{u} avoids phone calls and wants things in writing first"],
     "how should I contact {u}", ("phone call", "calling", "in writing")),
    (["{u} writes code indented with four spaces, never tabs",
      "Code style for {u}: 4 spaces per indent, no tabs",
      "{u} dislikes tab characters, spaces only in source files"],
     "which indentation style does {u} use in code", ("spaces",)),
    (["{u} uses dark mode in every editor and app",
      "Set everything to dark mode for {u}",
      "{u} needs dark themes because light mode gives {u} headaches"],
     "what theme does {u} use in the editor", ("dark mode", "dark theme")),
    (["{u} writes dates as year-month-day, like 2026-03-14",
      "Use ISO year-month-day dates with {u}",
      "{u} finds month-first dates confusing and wants year-month-day"],
     "which date format does {u} use", ("year-month-day",)),
    (["{u} wants a direct, informal tone with no corporate buzzwords",
      "Skip the buzzwords with {u}; plain direct language only",
      "{u} dislikes corporate jargon and wants plain talk"],
     "what tone does {u} like in writing", ("buzzword", "jargon")),
    (["{u} keeps mornings free for focus work and takes meetings after 1 pm",
      "No meetings before 1 pm for {u}, mornings are for deep work",
      "{u} protects the morning for focused work and meets only in the afternoon"],
     "when does {u} take meetings", ("1 pm", "afternoon")),
    (["{u} is vegetarian and does not eat fish either",
      "When planning meals or restaurants for {u}, vegetarian only and no fish",
      "{u} has been vegetarian for ten years, fish included"],
     "what are the dinner rules for {u}", ("vegetarian",)),
    (["{u} listens to instrumental jazz while working",
      "Put on instrumental jazz when {u} starts a work block",
      "{u} concentrates best with jazz and no lyrics"],
     "what music does {u} play while working", ("jazz",)),
    (["{u} goes running on Tuesday and Thursday mornings before work",
      "{u}'s running days are Tuesday and Thursday, early",
      "Keep Tuesday and Thursday mornings clear because {u} runs then"],
     "which mornings does {u} go running", ("tuesday and thursday",)),
    (["{u} signs emails with Best and never uses the word Regards",
      "For {u}'s email sign-off use Best, not Regards",
      "{u} dislikes Regards as a sign-off and writes Best"],
     "how does {u} sign off emails", ("regards",)),
    (["{u} keeps notes in plain markdown files, not in a notes app",
      "{u}'s notes live in markdown files on disk",
      "Do not suggest notes apps to {u}; markdown files only"],
     "where does {u} keep notes", ("markdown",)),
    (["{u} reads on an e-reader at night and avoids screens after 10 pm",
      "No screens for {u} after 10 pm; the e-reader is fine",
      "{u} uses an e-reader in the evening instead of a tablet"],
     "what does {u} read on in the evening", ("e-reader",)),
    (["{u} wants slide decks with at most five bullets per slide and one big chart",
      "Slides for {u}: five bullets maximum and a large chart",
      "{u} asks for decks with a single big chart and no more than five bullets a slide"],
     "how should slide decks for {u} look", ("five bullets",)),
    (["{u} does not work weekends and wants no reminders on Saturday or Sunday",
      "No weekend reminders for {u}, ever",
      "{u} keeps Saturday and Sunday free of work notifications"],
     "does {u} want reminders on weekends", ("weekend", "saturday")),
    (["{u} prefers trains over short flights when the trip is under four hours",
      "For trips under four hours book {u} on the train",
      "{u} would rather take the train than fly short distances"],
     "how does {u} prefer to travel short distances", ("train",)),
    (["{u} prefers Google Sheets over Excel for shared budgets",
      "Shared budget spreadsheets for {u} go in Google Sheets",
      "{u} does not want Excel files for budgets, only Google Sheets"],
     "which spreadsheet tool does {u} use for budgets", ("google sheets",)),
    (["{u} uses the 24-hour clock for all times",
      "Write times for {u} in 24-hour format",
      "{u} finds am and pm confusing and wants the 24-hour clock"],
     "which clock format does {u} use", ("24-hour",)),
    (["{u} uses a password manager and never wants passwords repeated in chat",
      "Never echo a password back to {u}; the password manager has them",
      "{u} keeps credentials in a password manager only"],
     "how does {u} handle passwords", ("password manager", "password")),
    (["{u} needs captions on every video that is shared",
      "Always include captions when sending {u} video",
      "{u} watches videos muted, so captions are a must"],
     "what does {u} need on videos", ("captions",)),
    (["{u} budgets in Canadian dollars and wants prices converted to CAD",
      "Convert prices to Canadian dollars for {u}",
      "{u} thinks in CAD, so convert foreign prices"],
     "which currency does {u} budget in", ("cad", "canadian dollars")),
    (["{u} prefers quiet hotels away from the main road even if farther from the venue",
      "Pick {u} a quiet hotel, distance to the venue matters less",
      "{u} sleeps badly near traffic, so book a quiet hotel"],
     "what kind of hotel does {u} prefer", ("quiet hotel",)),
    (["{u} likes agendas sent the evening before every meeting",
      "Send {u} the agenda the night before",
      "{u} wants the agenda the evening before, not in the room"],
     "when does {u} want meeting agendas", ("agenda",)),
    (["{u} wants code review comments as a numbered list with the severity first",
      "Number the review comments for {u} and lead each with the severity",
      "{u} reads review feedback best as a numbered list ordered by severity"],
     "how should code review comments be written for {u}", ("severity",)),
    (["{u} likes the weekly summary on Friday at four and not later",
      "{u}'s weekly summary is due Friday at four",
      "Deliver the weekly summary to {u} Friday afternoon at four"],
     "when does {u} want the weekly summary", ("friday at four", "friday afternoon")),
]

PROJECTS = ["Atlas migration", "Harbor mobile app", "Orchid data warehouse", "Lighthouse onboarding redesign",
            "Kestrel vendor review", "Juniper billing rewrite", "Beacon analytics rollout", "Cobalt security audit"]
SLUG = {"Atlas migration": "atlas", "Harbor mobile app": "harbor", "Orchid data warehouse": "orchid",
        "Lighthouse onboarding redesign": "lighthouse", "Kestrel vendor review": "kestrel",
        "Juniper billing rewrite": "juniper", "Beacon analytics rollout": "beacon", "Cobalt security audit": "cobalt"}
FIRST = ["Elena", "Marcus", "Noor", "Dmitri", "Chiara", "Kofi", "Ingrid", "Rafael", "Yuki", "Beatriz", "Ahmed",
         "Freya", "Mateo", "Zainab", "Lucas", "Anya", "Hugo", "Nadia", "Pavel", "Greta"]
LAST = ["Delgado", "Okonkwo", "Lindqvist", "Marchetti", "Volkov", "Tanaka", "Haddad", "Brennan", "Castillo",
        "Novak", "Ibrahim", "Fontaine", "Oyelaran", "Petrov", "Santoro", "Whitlock", "Zielinski", "Abara",
        "Moreau", "Kowalczyk", "Ferreira", "Nakamura", "Adeyemi", "Bergstrom"]
VENDORS = ["Quillfeather Systems", "Brightwater Logistics", "Ostrava Cloud", "Mapleton Payments", "Tidewell Software",
           "Granite Peak Consulting", "Larkspur Analytics", "Nimbus Harbor Hosting"]
STACKS = [("Kotlin", "Ktor"), ("Python", "FastAPI"), ("Go", "Gin"), ("TypeScript", "NestJS"), ("Ruby", "Rails"),
          ("Rust", "Axum"), ("Java", "Quarkus"), ("Elixir", "Phoenix")]
RISKS = ["unreliable legacy export job", "single login service nobody owns", "unclear data ownership between teams",
         "vendor sandbox that resets every night", "shared test database that drifts", "manual release checklist",
         "certificate that expires without a reminder", "contractor handover that is still missing"]
DEMO_DAYS = ["second Thursday", "last Wednesday", "first Friday", "third Tuesday", "every other Monday"]
SUFFIX = ["core", "api", "web", "etl", "infra", "mono", "tools", "docs"]
HOSTS = ["borealis", "cinder", "driftwood", "ember", "foxglove", "gannet", "heron", "ironbark"]

COMPANIES = ["Northwind Traders", "Contoso Labs", "Fabrikam Studios", "Initech Systems", "Globex Retail", "Hooli Health",
             "Umbra Robotics", "Vandelay Imports", "Stark Freight", "Wayne Optics"]
TITLES = ["junior analyst", "data analyst", "senior analyst", "analytics lead", "product manager", "senior product manager",
          "head of data", "engineering manager", "staff engineer", "operations director"]
CITIES = ["Montreal", "Lisbon", "Calgary", "Porto", "Halifax", "Leeds", "Ottawa", "Seville", "Winnipeg", "Bergen",
          "Dundee", "Valencia"]
STREETS = ["Maple Row", "Harbour Lane", "Quarry Street", "Linden Court", "Foundry Road", "Willow Terrace", "Mill Crescent",
           "Beacon Way", "Orchard Close", "Station Mews"]
TRACKERS = ["Jira", "Linear", "Asana", "Trello", "ClickUp", "YouTrack"]
DATABASES = ["Postgres", "MySQL", "MongoDB", "CockroachDB", "MariaDB", "SQL Server"]
EDITORS = ["VS Code", "Neovim", "IntelliJ", "Sublime Text", "Zed", "Emacs"]
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
TIMES = ["8:30 am", "9:00 am", "9:30 am", "10:00 am", "10:30 am", "11:00 am", "2:00 pm", "3:30 pm"]
AMOUNTS = ["$45k", "$60k", "$85k", "$120k", "$150k", "$210k", "$275k", "$340k"]

ALLERGENS = ["penicillin", "peanuts", "shellfish", "latex", "sesame", "walnuts", "sulfa drugs", "bee stings"]
KIDS = ["Nora", "Eli", "Mila", "Theo", "Iris", "Leo"]
MEDS = ["warfarin", "lithium", "an MAO inhibitor", "methotrexate"]
INTERACTS = ["grapefruit juice", "ibuprofen", "aged cheese", "St John's wort"]
PERMITS = ["work permit", "residence permit", "business licence", "import licence", "drone pilot licence"]
LABS = ["annex freezer", "basement freezer", "cold room", "tissue bank freezer"]
SAMPLES = ["cell line", "vaccine batch", "reagent stock", "tissue"]
DROP_REASONS = ["data hosted outside the EU", "unlimited liability on outages", "an auto-renewal clause with a 90 day notice",
                "exclusive rights over our exports", "audit access refused"]
DB_REASONS = ["row-level security the auditors asked for", "predictable costs under heavy reporting load",
              "the team already running it for payments", "read replicas that do not lag"]
DB_CONDITIONS = ["compliance changes", "the budget doubles", "a regulator objects", "we outgrow one region"]

# Throwaway frames. {u} user, {food} {place} {thing} {adj} {person} {show} {sport} {transport} {weather}
FOODS = ["ramen", "a falafel wrap", "leftover pasta", "a burrito", "tomato soup", "a poke bowl", "dumplings", "a club sandwich",
         "pad thai", "a mushroom pizza"]
PLACES = ["the corner bakery", "the new ramen place", "the food hall", "the park by the river", "the rooftop cafe",
          "the library annex", "the pier", "the farmers market", "the climbing gym", "the small cinema downtown"]
THINGS = ["umbrella", "bike lock", "headphones", "water bottle", "charger", "notebook", "scarf", "backpack", "sunglasses"]
ADJ = ["packed", "freezing", "weirdly empty", "far too loud", "slow", "full of tourists", "unusually calm", "stuffy"]
SHOWS = ["a baking competition", "a nature documentary", "a detective series", "an old sitcom", "a space drama",
         "a cooking show", "a heist movie", "a sports documentary"]
SPORTS = ["the playoff game", "a local football match", "the tennis final", "a cycling race", "the marathon", "a hockey game"]
TRANSPORT = ["tram", "bus", "subway", "ferry", "train", "shuttle"]
WEATHER = ["rained all morning", "was surprisingly warm", "turned foggy by noon", "was windy and grey", "was bright and cold"]
GADGETS = ["a smart plug", "a mechanical keyboard", "a standing desk mat", "a tiny projector", "a cable organizer",
           "a webcam light", "noise cancelling earbuds", "a second monitor arm"]
REMARK_FRAMES = [
    "{u} said the {transport} was {adj} this morning",
    "{u} mentioned having {food} for lunch near {place}",
    "{u} joked that {sport} lasted longer than it should have",
    "{u} is watching {show} this week",
    "{u} lost a {thing} somewhere around {place}",
    "{u} said {place} was {adj} today",
    "{u} mentioned that it {weather}",
    "{u} is thinking about buying {gadget} but has not decided",
    "{u} said the {thing} finally turned up in the car",
    "{u}'s cousin {person} is visiting for a few days",
    "{u} was late because the {transport} was delayed again",
    "{u} tried {food} at {place} and called it fine",
    "{u} mentioned a headache this afternoon that went away after lunch",
    "{u} said the neighbour's dog barked all night",
    "{u} is planning to rearrange the desk when there is time",
    "{u} heard a good podcast episode about {show} on the way in",
    "{u} said {person} recommended {place} for next Friday",
    "{u} spilled coffee on the {thing} and laughed about it",
    "{u} wondered aloud whether {sport} is worth watching live",
    "{u} bought {gadget} on sale and has not opened it yet",
]
SMALLTALK_FRAMES = [
    "{u} said good morning and asked how my day was going",
    "{u} thanked me for the quick answer",
    "{u} said that was exactly what they needed, thanks",
    "{u} sent a thumbs up after the summary",
    "{u} joked that Mondays should be illegal",
    "{u} said they would be back after lunch",
    "{u} wished me a good weekend",
    "{u} said they are off to a meeting and will pick this up later",
    "{u} laughed at the pun in the draft",
    "{u} said it has been a long week",
    "{u} asked me to say hello to the team, in a joking way",
    "{u} said coffee time and signed off for ten minutes",
    "{u} complimented the clear formatting of the last reply",
    "{u} sighed about the Friday afternoon traffic",
]
TASK_FRAMES = [
    "Reviewed pull request {n} for the {slug} repo",
    "Sent the invoice for order {n} to the client",
    "Booked meeting room {room} for Thursday at two",
    "Drafted a reply to {person} about the {thing} request",
    "Summarized the {slug} planning document for {u}",
    "Set a reminder for {u} to call the dentist",
    "Created ticket {key}-{n}: fix flaky login test",
    "Added {u}'s expense {n} for the taxi to the airport",
    "Checked the {slug} dashboard and everything looked normal",
    "Renamed the folder for the {slug} meeting notes",
    "Rescheduled the check-in with {person} to later in the week",
    "Looked up opening hours for {place}",
    "Pulled the numbers for report {n} and sent them to {u}",
    "Tidied the shared calendar and removed two duplicate invites",
]
DECOY_FRAMES = [
    ("allergen", "{u} read an article about how {x} is grown and sold"),
    ("allergen", "{u} said the label on the snack mentioned {x} in the fine print and laughed it off"),
    ("permit", "{u} saw a news story about queues at the {x} office"),
    ("lab", "{u} walked past the {x} on the way to the stairs"),
    ("vendor", "{u} said the {x} sales rep keeps sending friendly emails"),
]

CATEGORY_MIX = {
    "realistic": {
        "preference": [("preference", 0.75), ("personal", 0.10), ("other", 0.10), ("task", 0.05)],
        "project": [("project", 0.70), ("task", 0.15), ("other", 0.10), ("decision", 0.05)],
        "evolving": [("personal", 0.40), ("project", 0.40), ("preference", 0.10), ("other", 0.10)],
        "allergy": [("safety", 0.55), ("personal", 0.30), ("other", 0.15)],
        "legal": [("project", 0.35), ("decision", 0.15), ("personal", 0.20), ("task", 0.15), ("safety", 0.15)],
        "safety": [("safety", 0.60), ("personal", 0.20), ("project", 0.10), ("other", 0.10)],
        "decision": [("decision", 0.65), ("project", 0.25), ("other", 0.10)],
        "remark": [("other", 0.55), ("personal", 0.20), ("task", 0.10), ("preference", 0.08), ("project", 0.07)],
        "smalltalk": [("other", 0.75), ("personal", 0.15), ("task", 0.10)],
        "tasklog": [("task", 0.70), ("project", 0.20), ("other", 0.10)],
    },
    "clean": {
        "preference": [("preference", 1.0)], "project": [("project", 1.0)], "evolving": [("personal", 0.5), ("project", 0.5)],
        "allergy": [("safety", 1.0)], "legal": [("decision", 1.0)], "safety": [("safety", 1.0)], "decision": [("decision", 1.0)],
        "remark": [("other", 1.0)], "smalltalk": [("other", 1.0)], "tasklog": [("task", 1.0)],
    },
}


def pick(rng: random.Random, choices: list[tuple[str, float]]) -> str:
    roll, total = rng.random(), 0.0
    for name, weight in choices:
        total += weight
        if roll < total:
            return name
    return choices[-1][0]


# ---------------------------------------------------------------------------------------
# Workload generation
# ---------------------------------------------------------------------------------------

def build_workload(seed: int, weeks: int = 16, labels: str = "realistic", goals: bool = False) -> dict:
    """Events per simulated day: ("write", text, category, cls, fid, vidx) and
    ("query", text, fid). `facts` carries the ground truth."""

    rng = random.Random(seed)
    days = weeks * 7
    mix = CATEGORY_MIX[labels]
    user = rng.choice(USERS)
    events: list[list[tuple]] = [[] for _ in range(days)]
    facts: dict[str, dict] = {}

    def fill(text: str, **kw: str) -> str:
        return text.format(u=user, **kw)

    def put(day: int, event: tuple) -> None:
        events[max(0, min(days - 1, day))].append(event)

    def people(n: int) -> list[str]:
        firsts, lasts = rng.sample(FIRST, n), rng.sample(LAST, n)
        return [f"{a} {b}" for a, b in zip(firsts, lasts)]

    # -- preferences -------------------------------------------------------------------
    for index, (variants, query, markers) in enumerate(rng.sample(PREFERENCES, 24)):
        fid = f"pref{index}"
        first = rng.randint(0, int(days * 0.55))
        order = rng.sample(range(len(variants)), len(variants))
        again = [d for d in (first + rng.randint(9, 45), first + rng.randint(30, 70)) if d < days][: rng.choice([0, 0, 1, 2])]
        facts[fid] = {"kind": "preference", "versions": [markers], "mentions": 1 + len(again)}
        cat = pick(rng, mix["preference"])
        put(first, ("write", fill(variants[order[0]]), cat, "preference", fid, 0))
        for k, day in enumerate(again, start=1):
            put(day, ("write", fill(variants[order[k % len(variants)]]), pick(rng, mix["preference"]), "preference", fid, 0))
        for day in sorted({min(days - 1, first + rng.randint(12, 50)), rng.randint(int(days * 0.65), days - 1)}):
            if day > first:
                put(day, ("query", fill(query), fid))

    # -- project facts -------------------------------------------------------------------
    projects = rng.sample(PROJECTS, 4)
    for project in projects:
        slug = SLUG[project]
        sponsor, vendor_contact, lead = people(3)
        vendor = rng.choice(VENDORS)
        language, framework = rng.choice(STACKS)
        repo = f"{slug}-{rng.choice(SUFFIX)}"
        host = f"stg-{rng.choice(HOSTS)}"
        kickoff = f"{rng.choice(MONTHS)} {rng.randint(2, 27)}"
        team = rng.randint(3, 9)
        risk = rng.choice(RISKS)
        demo = rng.choice(DEMO_DAYS)
        defs = [
            ([f"{sponsor} is the executive sponsor for the {project}", f"The {project} reports to {sponsor}, the executive sponsor"],
             f"who is the executive sponsor for the {project}", (sponsor.split()[1].lower(),)),
            ([f"The {project} code lives in the repository {repo}", f"Repository for the {project}: {repo}"],
             f"which repository holds the {project} code", (repo,)),
            ([f"Staging for the {project} runs on {host}", f"The {project} staging environment is {host}"],
             f"where does staging for the {project} run", (host,)),
            ([f"{vendor_contact} at {vendor} handles billing questions for the {project}",
              f"For billing on the {project} talk to {vendor_contact} at {vendor}"],
             f"who handles billing questions for the {project}", (vendor_contact.split()[1].lower(),)),
            ([f"The {project} backend is written in {language} with {framework}",
              f"{project} backend stack: {language} and {framework}"],
             f"what is the backend stack of the {project}", (framework.lower(),)),
            ([f"The {project} kickoff meeting was held on {kickoff}", f"Kickoff for the {project} happened on {kickoff}"],
             f"when was the kickoff for the {project}", (kickoff.lower(),)),
            ([f"{lead} leads a team of {team} engineers on the {project}", f"The {project} engineering lead is {lead}, with {team} people"],
             f"who leads the engineering team on the {project}", (lead.split()[1].lower(),)),
            ([f"The {project} team runs a demo on the {demo} of the month",
              f"Demos for the {project} happen on the {demo}"],
             f"when does the {project} team run its demo", (demo.lower(),)),
            ([f"The biggest risk on the {project} is the {risk}", f"Main risk for the {project}: {risk}"],
             f"what is the biggest risk on the {project}", (risk,)),
        ]
        for index, (variants, query, markers) in enumerate(defs):
            if rng.random() < 0.15:
                continue
            fid = f"proj-{slug}-{index}"
            first = rng.randint(0, int(days * 0.65))
            again = [first + rng.randint(10, 40)] if rng.random() < 0.3 else []
            again = [d for d in again if d < days]
            facts[fid] = {"kind": "project", "versions": [markers], "mentions": 1 + len(again)}
            put(first, ("write", variants[0], pick(rng, mix["project"]), "project", fid, 0))
            for day in again:
                put(day, ("write", variants[1], pick(rng, mix["project"]), "project", fid, 0))
            for day in sorted({min(days - 1, first + rng.randint(10, 45)), rng.randint(int(days * 0.6), days - 1)}):
                if day > first:
                    put(day, ("query", query, fid))

    # -- evolving facts ------------------------------------------------------------------
    def evolve(fid: str, versions: list[tuple[list[str], str]], query: str) -> None:
        """versions: [(text variants [slot-friendly, free-form], marker)]. Later versions are
        stated weeks after the previous one; the current value is sometimes restated."""
        count = len(versions)
        gaps = sorted(rng.sample(range(2, days - 10), count - 1)) if count > 1 else []
        stamps = [rng.randint(0, 20)] + [max(g, 0) for g in gaps]
        stamps = sorted(stamps)
        for k in range(1, count):
            stamps[k] = max(stamps[k], stamps[k - 1] + 10)
        stamps = [min(s, days - 3) for s in stamps]
        facts[fid] = {"kind": "evolving", "versions": [(m.lower(),) for _, m in versions], "starts": stamps,
                      "mentions": count}
        for k, ((variants, _marker), day) in enumerate(zip(versions, stamps)):
            text = variants[0] if (k == 0 or rng.random() < 0.6 or len(variants) == 1) else variants[1]
            put(day, ("write", text, pick(rng, mix["evolving"]), "evolving", fid, k))
            for query_day in sorted({min(days - 1, day + rng.randint(3, 12)),
                                     min(days - 1, day + rng.randint(13, 35))}):
                nxt = stamps[k + 1] if k + 1 < count else days
                if day < query_day < nxt:
                    put(query_day, ("query", query, fid))
        if rng.random() < 0.5:  # the current value is restated in other words
            day = min(days - 1, stamps[-1] + rng.randint(8, 30))
            put(day, ("write", versions[-1][0][-1], pick(rng, mix["evolving"]), "evolving", fid, count - 1))
            facts[fid]["mentions"] += 1
        put(rng.randint(int(days * 0.85), days - 1), ("query", query, fid))

    k_for = lambda: rng.choice([2, 3, 3])  # noqa: E731
    titles = rng.sample(TITLES, 3)
    companies = rng.sample(COMPANIES, 2)
    cities = rng.sample(CITIES, 3)
    streets = rng.sample(STREETS, 3)
    proj_a, proj_b = projects[0], projects[1]
    deadlines = [f"{MONTHS[(i + 2) % 12]} {rng.randint(3, 27)}" for i in range(3)]
    deadlines_b = [f"{MONTHS[(i + 5) % 12]} {rng.randint(3, 27)}" for i in range(3)]
    trackers = rng.sample(TRACKERS, 3)
    databases = rng.sample(DATABASES, 3)
    editors = rng.sample(EDITORS, 3)
    times = rng.sample(TIMES, 3)
    amounts = rng.sample(AMOUNTS, 3)
    managers = people(3)
    n = k_for()
    evolve("ev-title", [
        ([f"{user} works as a {titles[0]} at {companies[0]}", f"{user}'s job title is {titles[0]}"], titles[0]),
        ([f"{user} was promoted and now works as a {titles[1]}", f"Update on {user}'s role: the new title is {titles[1]}, replacing {titles[0]}"], titles[1]),
        ([f"{user} now works as a {titles[2]} after another promotion", f"Another change for {user}: the title is now {titles[2]}"], titles[2]),
    ][:n], f"what is {user}'s current job title")
    n = rng.choice([2, 2, 3])
    evolve("ev-employer", [
        ([f"{user} works at {companies[0]}", f"{user}'s employer is {companies[0]}"], companies[0]),
        ([f"{user} now works at {companies[1]}", f"{user} left {companies[0]} and joined {companies[1]}"], companies[1]),
    ][:n], f"which company does {user} work for now")
    evolve("ev-city", [
        ([f"{user} lives in {cities[0]}", f"{user} is based in {cities[0]}"], cities[0]),
        ([f"{user} moved to {cities[1]}", f"Update: {user} has relocated, the new home city is {cities[1]}"], cities[1]),
        ([f"{user} lives in {cities[2]} now", f"{user} is now based in {cities[2]} after another move"], cities[2]),
    ][: k_for()], f"which city does {user} live in")
    evolve("ev-address", [
        ([f"{user}'s mailing address is {rng.randint(10, 99)} {streets[0]}", f"Send mail for {user} to {streets[0]}"], streets[0]),
        ([f"{user}'s new mailing address is {streets[1]}", f"Address change for {user}: mail now goes to {streets[1]}"], streets[1]),
    ], f"what is {user}'s mailing address")
    evolve("ev-deadline-a", [
        ([f"The {proj_a} deadline is {deadlines[0]}", f"Delivery date for the {proj_a} is {deadlines[0]}"], deadlines[0]),
        ([f"The {proj_a} deadline slipped to {deadlines[1]}", f"Heads up, the {proj_a} will not make the old date, new target {deadlines[1]}"], deadlines[1]),
        ([f"The {proj_a} deadline moved to {deadlines[2]}", f"Leadership changed the {proj_a} date again to {deadlines[2]}"], deadlines[2]),
    ][: k_for()], f"when is the {proj_a} deadline")
    evolve("ev-deadline-b", [
        ([f"The {proj_b} deadline is {deadlines_b[0]}", f"Delivery date for the {proj_b} is {deadlines_b[0]}"], deadlines_b[0]),
        ([f"The {proj_b} deadline moved to {deadlines_b[1]}", f"The {proj_b} got pushed, the new date is {deadlines_b[1]}"], deadlines_b[1]),
    ], f"when is the {proj_b} deadline")
    evolve("ev-tracker", [
        ([f"The team tracks issues in {trackers[0]}", f"Our issue tracker is {trackers[0]}"], trackers[0]),
        ([f"We switched issue tracking from {trackers[0]} to {trackers[1]}", f"The team moved off {trackers[0]}, issues now live in {trackers[1]}"], trackers[1]),
        ([f"We switched issue tracking from {trackers[1]} to {trackers[2]}", f"Issue tracking changed again, it is {trackers[2]} now"], trackers[2]),
    ][: k_for()], "which tool does the team use to track issues")
    evolve("ev-database", [
        ([f"Our primary database is {databases[0]}", f"The main database runs on {databases[0]}"], databases[0]),
        ([f"We migrated the primary database from {databases[0]} to {databases[1]}", f"The main database is now {databases[1]} after the migration"], databases[1]),
    ], "which database is the primary one")
    evolve("ev-standup", [
        ([f"The daily standup is at {times[0]}", f"Standup happens every day at {times[0]}"], times[0]),
        ([f"The daily standup moved to {times[1]}", f"Standup got shifted, it is at {times[1]} now"], times[1]),
        ([f"The daily standup moved to {times[2]}", f"Standup time changed again to {times[2]}"], times[2]),
    ][: k_for()], "what time is the daily standup")
    evolve("ev-budget", [
        ([f"The {proj_a} budget is {amounts[0]}", f"Budget for the {proj_a}: {amounts[0]}"], amounts[0]),
        ([f"The {proj_a} budget was revised to {amounts[1]}", f"Finance revised the {proj_a} budget, it is {amounts[1]} now"], amounts[1]),
    ], f"what is the budget for the {proj_a}")
    evolve("ev-editor", [
        ([f"{user} uses {editors[0]} as the main code editor", f"{user}'s main editor is {editors[0]}"], editors[0]),
        ([f"{user} switched from {editors[0]} to {editors[1]} as the main editor", f"{user} now prefers {editors[1]} for coding"], editors[1]),
    ], f"which code editor does {user} use")
    evolve("ev-manager", [
        ([f"{user}'s manager is {managers[0]}", f"{user} reports to {managers[0]}"], managers[0].split()[1]),
        ([f"{user}'s new manager is {managers[1]} after the reorg", f"After the reorg {user} reports to {managers[1]} instead"], managers[1].split()[1]),
    ], f"who is {user}'s manager")

    # -- critical, mentioned once, asked much later --------------------------------------
    allergen, allergen2 = rng.sample(ALLERGENS, 2)
    kid, med, interact = rng.choice(KIDS), rng.choice(MEDS), rng.choice(INTERACTS)
    permit, lab, sample = rng.choice(PERMITS), rng.choice(LABS), rng.choice(SAMPLES)
    legal_a = f"{rng.choice(MONTHS)} {rng.randint(2, 27)}, 2027"
    legal_b = f"{rng.choice(MONTHS)} {rng.randint(2, 27)}, 2027"
    company = rng.choice(COMPANIES)
    vendor_drop, reason = rng.choice(VENDORS), rng.choice(DROP_REASONS)
    db_pick, db_other = rng.sample(DATABASES, 2)
    db_reason, db_cond = rng.choice(DB_REASONS), rng.choice(DB_CONDITIONS)
    crit_defs = [
        ("allergy", f"{user} is severely allergic to {allergen} and carries an epinephrine pen",
         f"what is {user} allergic to", (allergen,)),
        ("allergy", f"{user}'s daughter {kid} has a serious {allergen2} allergy, so never suggest recipes or restaurants that use it",
         f"what food must never be suggested for {kid}", (allergen2,)),
        ("safety", f"{user} takes {med} daily and must never combine it with {interact}, flag any plan that does",
         f"what must {user} avoid combining with {med}", (interact,)),
        ("legal", f"The {permit} renewal has to be filed by {legal_a}, a late filing voids the permit",
         f"when is the {permit} renewal due", (legal_a.lower(),)),
        ("legal", f"The tax filing for {company} is legally due on {legal_b} and penalties start the next day",
         f"when is the tax filing for {company} due", (legal_b.lower(),)),
        ("safety", f"Never leave the {lab} alarm silenced overnight, the {sample} inside spoils and cannot be replaced",
         f"what is the rule about the {lab} alarm", (lab,)),
        ("decision", f"Decided to drop vendor {vendor_drop} because their contract requires {reason}, which legal rejected",
         f"why did we drop {vendor_drop}", (reason,)),
        ("decision", f"Chose {db_pick} over {db_other} for reporting because of {db_reason}, do not revisit unless {db_cond}",
         f"why did we choose {db_pick} for reporting", (db_reason,)),
    ]
    chosen = rng.sample(crit_defs, 6)
    for index, (kind, text, query, markers) in enumerate(chosen):
        fid = f"crit{index}"
        facts[fid] = {"kind": "critical", "versions": [markers], "mentions": 1}
        put(rng.randint(2, int(days * 0.35)), ("write", text, pick(rng, mix[kind]), "critical", fid, 0))
        for day in sorted({rng.randint(int(days * 0.72), int(days * 0.85)), rng.randint(int(days * 0.88), days - 1)}):
            put(day, ("query", query, fid))
    decoy_for = {"allergen": allergen, "permit": permit, "lab": lab, "vendor": vendor_drop}

    # -- throwaways ----------------------------------------------------------------------
    def common_fill(frame: str) -> str:
        return frame.format(
            u=user, food=rng.choice(FOODS), place=rng.choice(PLACES), thing=rng.choice(THINGS), adj=rng.choice(ADJ),
            person=rng.choice(FIRST), show=rng.choice(SHOWS), sport=rng.choice(SPORTS), transport=rng.choice(TRANSPORT),
            weather=rng.choice(WEATHER), gadget=rng.choice(GADGETS), n=rng.randint(100, 9999), room=rng.randint(2, 40),
            slug=SLUG[rng.choice(projects)], key=rng.choice(["OPS", "WEB", "DAT", "FIN"]),
        )

    for index in range(int(days * 1.5)):
        put(rng.randint(0, days - 1), ("write", common_fill(rng.choice(REMARK_FRAMES)), pick(rng, mix["remark"]),
                                       "throwaway", f"x{index}", 0))
    for index in range(int(days * 0.7)):
        put(rng.randint(0, days - 1), ("write", common_fill(rng.choice(SMALLTALK_FRAMES)), pick(rng, mix["smalltalk"]),
                                       "throwaway", f"s{index}", 0))
    for index in range(int(days * 1.1)):
        put(rng.randint(0, days - 1), ("write", common_fill(rng.choice(TASK_FRAMES)), pick(rng, mix["tasklog"]),
                                       "throwaway", f"t{index}", 0))
    for index, (key, frame) in enumerate(rng.sample(DECOY_FRAMES, 3)):
        put(rng.randint(int(days * 0.3), int(days * 0.7)), ("write", frame.format(u=user, x=decoy_for[key]),
                                                            pick(rng, mix["remark"]), "throwaway", f"d{index}", 0))
    if goals:
        add_goals(seed, user, days, mix, facts, put)
    for fact in facts.values():
        fact["versions"] = [tuple(m.lower() for m in markers) for markers in fact["versions"]]
    for day in events:
        rng.shuffle(day)
        day.sort(key=lambda e: e[0] == "query")  # a day's writes come before its queries
    return {"user": user, "days": days, "events": events, "facts": facts}


TAG_P_SERVING = 0.5
TAG_P_DISTRACTOR = 0.1

GOAL_MIX = {
    "goal": [("project", 0.40), ("personal", 0.30), ("other", 0.20), ("task", 0.10)],
    "serving": [("task", 0.35), ("other", 0.30), ("project", 0.25), ("personal", 0.10)],
}


def add_goals(seed: int, user: str, days: int, mix: dict, facts: dict, put) -> None:
    """Round 7 goal variant: 3 goals per simulated user, each with 4 serving facts (said once,
    asked once near the end) and 4 distractors that share words with the goal. Uses its own
    random stream, so the rest of the workload is the same as without goals."""

    from gates_goal_pool import GOALS

    rng = random.Random(seed * 7919 + 17)
    # Round 11 caller-tag model (declared in TUNING_R11_GRID.md before any r11 run): a caller tags a
    # goal-serving fact with its goal's id with probability TAG_P_SERVING and, by mistake, a
    # distractor with probability TAG_P_DISTRACTOR. Own random stream: the workload is unchanged,
    # and arms without MEMORYSAFE_GATE_GOAL2 ignore the tag.
    tag_rng = random.Random(seed * 104729 + 31)
    for g_index, (variants, serving, distractors) in enumerate(rng.sample(GOALS, 3)):
        goal_day = rng.randint(0, int(days * 0.10))
        put(goal_day, ("write", rng.choice(variants).format(u=user), pick(rng, GOAL_MIX["goal"]), "goal", f"goal{g_index}", 0))
        for s_index, (text, query, marker) in enumerate(serving):
            fid = f"goal{g_index}-s{s_index}"
            facts[fid] = {"kind": "goal_serving", "versions": [(marker,)], "mentions": 1}
            tagged = tag_rng.random() < TAG_P_SERVING
            put(rng.randint(goal_day + 1, int(days * 0.45)),
                ("write", text.format(u=user), pick(rng, GOAL_MIX["serving"]), "goal_serving", fid, 0,
                 f"goal{g_index}" if tagged else None))
            put(rng.randint(int(days * 0.85), days - 1), ("query", query.format(u=user), fid))
        for d_index, (_kind, text) in enumerate(distractors):
            tagged = tag_rng.random() < TAG_P_DISTRACTOR
            put(rng.randint(int(days * 0.10), int(days * 0.70)),
                ("write", text.format(u=user), pick(rng, mix["remark"]), "goal_distractor", f"goal{g_index}-d{d_index}", 0,
                 f"goal{g_index}" if tagged else None))


# ---------------------------------------------------------------------------------------
# Arms
# ---------------------------------------------------------------------------------------

ARMS: dict[str, dict | None] = {
    "fifo": {"_fifo": True},
    "shipped": {"MEMORYSAFE_MAX_ACTIVE": "{cap}"},
    "gate_cap": {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_MAX_ACTIVE": "{cap}"},
    "gate_cap+sur": {"MEMORYSAFE_GATE_CAPACITY": "1", "MEMORYSAFE_GATE_SURPRISE": "1", "MEMORYSAFE_GATE_MAX_ACTIVE": "{cap}"},
    "unbounded": {},
}
GATE_ARMS = ("gate_cap", "gate_cap+sur")


class Clock:
    """Simulated time: every storage timestamp comes from here, so 'older' means older."""

    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> str:
        return self.now.isoformat(timespec="seconds")


def run_one(arm: str, seed: int, weeks: int, capacity: int, labels: str, goals: bool = False) -> dict:
    work = build_workload(seed, weeks, labels, goals)
    facts, events = work["facts"], work["events"]
    spec = ARMS[arm]
    fifo = bool(spec and spec.get("_fifo"))
    clock = Clock()
    with tempfile.TemporaryDirectory() as temp:
        base = {"MEMORYSAFE_DB_PATH": str(Path(temp) / "d" / "m.sqlite3"), "MEMORYSAFE_STATE_DIR": str(Path(temp) / "s")}
        env = {k: v.format(cap=capacity) for k, v in (spec or {}).items() if not k.startswith("_")}
        saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("MEMORYSAFE_GATE_") or k == "MEMORYSAFE_MAX_ACTIVE"}
        gates._INDEXES.clear()
        try:
            with patch.dict(os.environ, {**base, **env}), patch.object(storage, "_now", clock):
                store = MemoryStore(Path(base["MEMORYSAFE_DB_PATH"]))
                return play(store, arm, work, facts, events, clock, capacity, fifo)
        finally:
            os.environ.update(saved)


def play(store: MemoryStore, arm: str, work: dict, facts: dict, events: list, clock: Clock, capacity: int, fifo: bool) -> dict:
    origin: dict[str, tuple[str, int, str]] = {}   # memory id -> (fact id, version, class) of the write that created it
    ids_by_class: dict[str, set[str]] = {}
    throwaway_writes = 0
    wrong_merges = right_merges = harmless_merges = 0
    ms: list[float] = []
    hits: dict[str, list[int]] = {"preference": [], "project": [], "evolving": [], "critical": [], "repeated": []}
    correct, leak, change_queries = [], [], 0
    goal_ids: dict[str, str] = {}   # round 11: goal fid -> its memory id (for caller tags)
    tagged_fids: set[str] = set()
    distractor_mid: dict[str, str] = {}

    def has(text: str, markers: tuple) -> bool:
        low = text.lower()
        return any(m in low for m in markers)

    for day, day_events in enumerate(events):
        clock.now = START + timedelta(days=day)
        for event in day_events:
            clock.now += timedelta(minutes=7, seconds=3)
            if event[0] == "write":
                _, text, category, cls, fid, vidx = event[:6]
                tag_goal = event[6] if len(event) > 6 else None
                kwargs = {}
                if tag_goal is not None and tag_goal in goal_ids:
                    kwargs["tag"] = f"goal:{goal_ids[tag_goal]}"
                    tagged_fids.add(fid)
                start = time.perf_counter()
                result = store.remember(text, category, **kwargs)
                ms.append(1000 * (time.perf_counter() - start))
                memory_id = result["memory_id"]
                if cls == "throwaway":
                    throwaway_writes += 1
                if result["decision"] == "MERGE":
                    source = origin.get(memory_id)
                    if source is not None and source[:2] == (fid, vidx):
                        right_merges += 1       # the same fact, same version, said again
                    elif cls == "throwaway" and source is not None and source[2] == "throwaway":
                        harmless_merges += 1    # two throwaway remarks folded: nothing of value lost
                    else:
                        wrong_merges += 1       # a fact (or version of a fact) folded into something else
                else:
                    origin[memory_id] = (fid, vidx, cls)
                    ids_by_class.setdefault(cls, set()).add(memory_id)
                if cls == "goal":
                    goal_ids[fid] = memory_id
                if cls == "goal_distractor" and result["decision"] != "MERGE":
                    distractor_mid[memory_id] = fid
                if fifo:
                    fifo_trim(store, capacity)
            else:
                _, query, fid = event
                fact = facts[fid]
                results = store.find(query, 3)
                texts = [r["content"] for r in results]
                if fact["kind"] == "evolving":
                    current = max(k for k, start_day in enumerate(fact["starts"]) if start_day <= day)
                    markers = fact["versions"][current]
                    older = tuple(m for k, ms_ in enumerate(fact["versions"]) if k != current for m in ms_)
                    hit = any(has(t, markers) for t in texts)
                    hits["evolving"].append(int(hit))
                    if current >= 1:
                        change_queries += 1
                        top = texts[0] if texts else ""
                        correct.append(int(has(top, markers)))
                        leak.append(int(has(top, older) and not has(top, markers)))
                else:
                    hit = any(has(t, fact["versions"][0]) for t in texts)
                    hits.setdefault(fact["kind"], []).append(int(hit))
                    if fact["mentions"] >= 2 and fact["kind"] in {"preference", "project"}:
                        hits["repeated"].append(int(hit))

    with store._session() as connection:
        active_rows = connection.execute("SELECT id, content FROM memories WHERE state = 'active'").fetchall()
    active = {r[0]: r[1] for r in active_rows}
    crit = [f for f, fact in facts.items() if fact["kind"] == "critical"]
    survived = sum(any(has(c, facts[f]["versions"][0]) for c in active.values()) for f in crit)
    throwaways_kept = sum(1 for i in ids_by_class.get("throwaway", ()) if i in active)
    useful_ids = {i for c in ("preference", "project", "evolving", "critical") for i in ids_by_class.get(c, ())}
    useful_facts = [f for f, fact in facts.items() if fact["kind"] in ("preference", "project", "evolving")]
    kept_facts = sum(any(has(c, m) for c in active.values() for m in [facts[f]["versions"][-1]]) for f in useful_facts)

    def rate(values: list[int]) -> float | None:
        return sum(values) / len(values) if values else None

    extra = {}
    serving = [f for f, fact in facts.items() if fact["kind"] == "goal_serving"]
    if serving:  # round-7 goal variant
        extra = {
            "goal_serving_survival": sum(any(has(c, facts[f]["versions"][0]) for c in active.values()) for f in serving) / len(serving),
            "hit_goal_serving": rate(hits.get("goal_serving", [])),
            "goal_distractor_retained": sum(1 for i in ids_by_class.get("goal_distractor", ()) if i in active)
            / max(1, len(ids_by_class.get("goal_distractor", ()))),
            "goal_memories_retained": sum(1 for i in ids_by_class.get("goal", ()) if i in active) / max(1, len(ids_by_class.get("goal", ()))),
        }
        # round 11 (secondary, descriptive): split by the caller-tag model
        untagged = [f for f in serving if f not in tagged_fids]
        tagged = [f for f in serving if f in tagged_fids]
        d_un = [i for i in ids_by_class.get("goal_distractor", ()) if distractor_mid.get(i) not in tagged_fids]
        extra.update({
            "goal_serving_survival_untagged": (sum(any(has(c, facts[f]["versions"][0]) for c in active.values()) for f in untagged)
                                               / len(untagged)) if untagged else None,
            "goal_serving_survival_tagged": (sum(any(has(c, facts[f]["versions"][0]) for c in active.values()) for f in tagged)
                                             / len(tagged)) if tagged else None,
            "goal_distractor_retained_untagged": (sum(1 for i in d_un if i in active) / len(d_un)) if d_un else None,
            "caller_tags": len(tagged_fids),
        })
    return {
        **extra,
        "hit_preference": rate(hits["preference"]), "hit_project": rate(hits["project"]),
        "hit_evolving": rate(hits["evolving"]), "hit_critical": rate(hits["critical"]), "hit_repeated": rate(hits["repeated"]),
        "critical_survival": survived / len(crit), "latest_fact_correct": rate(correct), "stale_leak": rate(leak),
        "wrong_merges": wrong_merges, "right_merges": right_merges, "harmless_merges": harmless_merges,
        "throwaways_retained": throwaways_kept / max(1, throwaway_writes),
        "throwaway_share_of_store": sum(1 for i in active if i in ids_by_class.get("throwaway", ())) / max(1, len(active)),
        "useful_facts_kept": kept_facts / max(1, len(useful_facts)),
        "write_ms_mean": statistics.mean(ms), "write_ms_p95": sorted(ms)[int(0.95 * (len(ms) - 1))], "writes": len(ms),
        "active_at_end": len(active), "change_queries": change_queries,
    }


def fifo_trim(store: MemoryStore, capacity: int) -> None:
    """Oldest active memory out (protected or not) -- the policy this script runs for the fifo arm."""
    with store._session() as connection:
        extra = int(connection.execute("SELECT COUNT(*) FROM memories WHERE state = 'active'").fetchone()[0]) - capacity
        if extra > 0:
            connection.execute(
                "UPDATE memories SET state = 'evicted' WHERE id IN "
                "(SELECT id FROM memories WHERE state = 'active' ORDER BY rowid LIMIT ?)", (extra,))


# ---------------------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------------------

# metric -> (label, higher_is_better, margin)
QUALITY = {
    "hit_preference": ("recall hit: preference", True, 0.02),
    "hit_project": ("recall hit: project fact", True, 0.02),
    "hit_evolving": ("recall hit: evolving (current value)", True, 0.02),
    "hit_critical": ("recall hit: rare-critical", True, 0.02),
    "hit_repeated": ("recall hit: repeated-mention facts", True, 0.02),
    "critical_survival": ("rare-critical survival (end)", True, 0.02),
    "latest_fact_correct": ("latest-fact correct (top-1)", True, 0.02),
    "stale_leak": ("stale-fact leakage (top-1)", False, 0.02),
    "wrong_merges": ("wrong merges per run", False, 0.5),
    "throwaways_retained": ("throwaways retained", False, 0.02),
}
EXTRA = {"throwaway_share_of_store": "throwaway share of final store", "useful_facts_kept": "useful facts still active (end)",
         "right_merges": "same-fact merges per run", "harmless_merges": "throwaway-into-throwaway merges", "active_at_end": "active at end"}


def mean(values: list[float | None]) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.mean(values) if values else None


def paired(arm_runs: list[dict], base_runs: list[dict], metric: str) -> tuple[float, float] | None:
    diffs = [a[metric] - b[metric] for a, b in zip(arm_runs, base_runs) if a[metric] is not None and b[metric] is not None]
    if len(diffs) < 2:
        return None
    return statistics.mean(diffs), statistics.stdev(diffs) / math.sqrt(len(diffs))


def verdict(runs: dict[str, list[dict]], arm: str) -> dict:
    base = runs["shipped"]
    better, worse = [], []
    for metric, (label, high, margin) in QUALITY.items():
        stat = paired(runs[arm], base, metric)
        if stat is None:
            continue
        diff, se = stat
        gain = diff if high else -diff
        if gain >= margin and gain > 2 * se:
            better.append(metric)
        elif gain <= -margin and gain < -2 * se:
            worse.append(metric)
    speed = paired([{"m": r["write_ms_mean"]} for r in runs[arm]], [{"m": r["write_ms_mean"]} for r in base], "m")
    faster = bool(speed and speed[0] < 0 and -speed[0] > 2 * speed[1])
    smarter = bool(better) and not worse
    if smarter and faster:
        label = "faster+smarter"
    elif smarter:
        label = "smarter only"
    elif worse and len(worse) > len(better):
        label = "worse"
    else:
        label = "exploratory"
    return {"verdict": label, "faster": faster, "smarter": smarter, "better": better, "worse": worse,
            "speed_diff_ms": None if speed is None else round(speed[0], 3)}


def run(seeds: list[int], arms: list[str], weeks: int, capacity: int, labels: str) -> dict:
    runs: dict[str, list[dict]] = {arm: [] for arm in arms}
    for seed in seeds:
        order = list(arms)
        random.Random(seed).shuffle(order)  # arm order differs per seed so warm-up does not favour one arm
        for arm in order:
            runs[arm].append(run_one(arm, seed, weeks, capacity, labels))
    table = {}
    for arm in arms:
        rows = runs[arm]
        entry = {m: mean([r[m] for r in rows]) for m in list(QUALITY) + list(EXTRA)}
        entry["write_ms_mean"] = mean([r["write_ms_mean"] for r in rows])
        entry["write_ms_p95"] = mean([r["write_ms_p95"] for r in rows])
        entry["change_queries"] = mean([r["change_queries"] for r in rows])
        entry["per_seed"] = {m: [r[m] for r in rows] for m in ("hit_critical", "critical_survival", "latest_fact_correct", "stale_leak")}
        table[arm] = entry
    out = {"seeds": seeds, "weeks": weeks, "capacity": capacity, "labels": labels, "arms": table, "verdicts": {}}
    if "shipped" in runs:
        for arm in arms:
            if arm not in ("shipped", "unbounded"):
                out["verdicts"][arm] = verdict(runs, arm)
    return out


def fmt(value: float | None, pct: bool = True) -> str:
    if value is None:
        return "   -"
    return f"{100 * value:5.1f}%" if pct else f"{value:6.2f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, default=8)
    parser.add_argument("--tune", action="store_true", help="use the tuning seeds 100..104")
    parser.add_argument("--weeks", type=int, default=16)
    parser.add_argument("--capacity", type=int, default=200)
    parser.add_argument("--labels", choices=["realistic", "clean"], default="realistic")
    parser.add_argument("--arms", nargs="*", default=list(ARMS))
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    seeds = list(range(100, 105)) if args.tune else list(range(args.seeds))
    arms = list(dict.fromkeys(["shipped"] + args.arms))
    report = run(seeds, arms, args.weeks, args.capacity, args.labels)
    print(f"Realistic assistant-memory benchmark: {args.weeks} weeks, capacity {args.capacity}, labels={args.labels}, "
          f"seeds {seeds[0]}..{seeds[-1]} ({len(seeds)}). Synthetic; internal design evidence only.\n")
    names = list(report["arms"])
    print(f"  {'metric':40s}" + "".join(f"{a:>14s}" for a in names))
    for metric, (label, high, _m) in QUALITY.items():
        arrow = "up" if high else "down"
        pct = metric != "wrong_merges"
        print(f"  {label + ' (' + arrow + ')':40s}" + "".join(f"{fmt(report['arms'][a][metric], pct):>14s}" for a in names))
    for metric, label in EXTRA.items():
        pct = metric in {"throwaway_share_of_store", "useful_facts_kept"}
        print(f"  {label:40s}" + "".join(f"{fmt(report['arms'][a][metric], pct):>14s}" for a in names))
    print(f"  {'write latency mean ms':40s}" + "".join(f"{fmt(report['arms'][a]['write_ms_mean'], False):>14s}" for a in names))
    print(f"  {'write latency p95 ms':40s}" + "".join(f"{fmt(report['arms'][a]['write_ms_p95'], False):>14s}" for a in names))
    print(f"  (change queries per run, used for latest-fact / stale-leak: {report['arms']['shipped']['change_queries']:.1f})\n")
    for arm, v in report["verdicts"].items():
        print(f"  verdict {arm:14s}: {v['verdict']:15s} faster={v['faster']} smarter={v['smarter']} "
              f"better={v['better'] or '-'} worse={v['worse'] or '-'} speed diff vs shipped {v['speed_diff_ms']} ms/write")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
