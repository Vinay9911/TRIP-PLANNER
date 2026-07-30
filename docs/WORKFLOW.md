# How it works, step by step

A plain-language walkthrough of what happens between someone sending a message
and getting a reply. No engineering background needed — technical terms are
explained the first time they appear.

If you want the engineering detail instead, read
[ARCHITECTURE.md](ARCHITECTURE.md).

---

## The short version

Someone types *"Plan me 2 relaxed days in Kyoto, I'm vegetarian."* Roughly
15 seconds later they get a two-day itinerary with real places, grouped by
neighbourhood, with vegetarian restaurants, and a note about rain on the
second afternoon.

In between, the system does nine things:

1. Checks who is asking
2. Remembers what it already knows about them
3. Works out what they actually want
4. Decides: ask a question, talk it through, or get straight to work
5. Writes a plan
6. Carries out the plan, one step at a time, using tools
7. Checks after each step whether the plan still makes sense
8. Writes the answer, in their language
9. Quietly learns something new about them, after they have their answer

The rest of this document walks through each.

---

## The worked example

We'll follow one message the whole way through.

> **User:** *Plan me 2 relaxed days in Kyoto. I'm vegetarian.*

Assume this person has used the system before, and once mentioned that they
dislike crowded tourist sites.

---

## Step 1 — Who is asking?

Every message arrives with a **token**: a small signed credential the user's
browser got when they logged in, a bit like a wristband at a venue.

The system checks the signature to be sure the token is genuine and has not
been altered. It does this with a public key it has already downloaded, so it
does not need to phone the login service on every message — faster, and it
keeps working if the login service has a brief wobble.

**Why this matters:** the user's identity from this token is what everything
downstream trusts. If someone could forge it, they could read other people's
conversations. So it is checked properly, every time, and taken from nowhere
else.

**Result:** *This is user 4f2a…, and the token is genuine.*

---

## Step 2 — What do we already know about them?

Before reading the new message properly, the system looks up what it has
learned about this person in **previous conversations**.

It does this by meaning, not by keyword. The stored note *"Traveller dislikes
crowded tourist sites"* will be found even though the new message contains none
of those words. (This works by turning text into lists of numbers that capture
meaning — similar meanings produce similar numbers.)

There is one important exception to "search by meaning". **Hard requirements —
dietary needs, allergies, accessibility, pets — are always loaded, every
time, regardless of whether they seem relevant.**

Here is why that matters. *"Plan me two days in Kyoto"* has almost nothing in
common, word-wise, with *"Traveller is allergic to nuts"*. A pure
search-by-meaning would rank the allergy below a note about liking temples and
quietly drop it. For a preference that would be a minor miss. For an allergy it
could be dangerous. So those are never left to a ranking.

**Result:**
- *(hard requirement)* — none stored yet for this person
- *(preference)* Traveller dislikes crowded tourist sites

---

## Step 3 — What are they actually asking for?

Now the system reads the message and pulls out the specifics:

| | |
|---|---|
| Destination | Kyoto |
| Length | 2 days |
| Dates | not stated |
| Style | relaxed |
| Requirement | vegetarian |
| Language | English |

The language matters — it decides what language the reply is written in. This
works for any language, and we come back to it in Step 8.

---

## Step 4 — Question, conversation, or straight to work?

The system now picks one of **three gears**, not two. This is the part of the
design that changed the most after watching a real transcript go wrong: a
message as short as *"I want to go to Kerala"* used to fall straight into the
full nine-step pipeline below and come back with a complete two-day itinerary
for districts the traveller had never chosen — impressively fast, and not
what anyone actually asked for. A travel agent handed that sentence talks to
you first.

**Gear 1 — ask a question.** Reserved for when planning would genuinely be
wasted otherwise: no destination at all, or a requirement so ambiguous that
guessing wrong ruins the whole itinerary. *"I want to go somewhere nice"* gets
*"Which city or country did you have in mind?"* and stops there.

**Gear 2 — talk it through.** A destination is named, but the trip itself
isn't shaped yet — no length, no timing, nothing to plan against. Rather than
either interrogating the traveller or guessing a whole itinerary for them, the
system does one lightweight lookup (real districts and areas for that place,
the same source the full plan uses — nothing invented) and replies with a
handful of genuinely different ways to experience it, plus at most two
questions:

> *"i want to go to kerala"* →
> *"You could chase the backwaters around Alappuzha 🌴, the tea hills near
> Munnar 🏔️, or the old streets of Fort Kochi 🏛️ — how many days do you have,
> and what matters most: nature, food, or culture? Say 'just plan it' any time
> and I'll build the full plan."*

This costs a fraction of a full plan — one lookup and one reply, not nine
steps — and it's what makes the toggles described in Step 6 discoverable: the
system mentions, once, that it can also check weather, compare flights, and
find restaurants and attractions once the trip has some shape.

**Gear 3 — get straight to work.** This is what our worked example gets,
and it's worth being precise about *why*, because two different signals both
lead here on their own:

- **The trip is already specified.** A length plus some sense of timing is
  enough — exact dates are not required, "early October" counts.
- **The words themselves are a command, not just interest.** *"Plan me 2
  relaxed days in Kyoto"* opens with an imperative and already gives a
  length — that is an instruction to build the trip, unlike *"I want to go to
  Kerala"*, which is just naming a place. Once someone says *"just plan it"*
  or accepts a proposed outline, gear 3 also stays selected for the rest of
  that conversation, so a later edit like *"make day 2 lighter"* refines the
  plan instead of restarting the conversation in Step 4.

**Whichever gear runs, it never asks about something it already knows.** This
is why Step 2 happens before Step 4, and why what a conversation has already
established (destination, days, who's coming) is never asked twice either. If
the two were built separately, the system would ask *"what's your budget?"*
to someone who answered that three conversations ago — exactly the annoyance
that remembering people is supposed to eliminate.

**Result for our example:** *"Plan me 2 relaxed days in Kyoto. I'm
vegetarian"* is both specified (a length) and a command (the verb "Plan me").
Gear 3. Straight to Step 5.

---

## Step 5 — Write a plan

The system now writes down what it intends to do, before doing any of it:

> 1. Research which Kyoto neighbourhoods suit a slow pace and quiet streets
> 2. Find vegetarian places to eat in those neighbourhoods
> 3. Check the weather for the trip
> 4. Write the itinerary

**Why plan at all,** rather than just improvising? Three reasons:

- **It stops the system wandering.** Improvising means deciding what to do next
  after every single result, with no sense of when to stop.
- **You can read it.** The plan is saved. Anyone can look at what the system
  decided to do and disagree with it. "It called some tools and produced text"
  cannot be reviewed.
- **It can be repaired.** When a step fails, there is a specific thing to fix.

Plans are kept short — two to four steps. A longer plan is usually a sign of
over-thinking, and each step costs time the user is waiting through.

---

## Step 6 — Carry out the plan

Steps run one at a time. For each, the system is told the goal, that one step,
and what earlier steps found — **but not the rest of the plan**, because
otherwise it runs ahead and tries to do everything at once.

It then picks its own tools. It has eight:

| Tool | What it does |
|---|---|
| Travel guide | Reads Wikivoyage, a free travel guide written by travellers |
| Places | Finds real venues with addresses, from OpenStreetMap |
| Weather | Real forecasts |
| Web search | Current things: events, closures, prices |
| Flights / Hotels | Travel and accommodation options |
| Remember / Recall | Looks up or saves facts about the traveller |

**Nothing here is pre-programmed.** The system is not told "if the message
mentions weather, check the weather." It is given a description of each tool
and chooses. Ask *"will I need an umbrella?"* and it checks weather only. Ask
for a full itinerary and it uses four or five.

**One exception, and it's a deliberate one.** The chat screen has toggles for
Flights / Attractions / Stays / Restaurants. Switch one off and it isn't a
hint — the tool for it is physically removed from the list above before the
system ever sees the request, the same way the memory tools disappear
entirely for someone who's turned memory off. Someone who only wants a
walking itinerary can switch off Flights and Stays and never get a flight
suggestion, however good an idea the model might otherwise think it is.

### What the travel guide tool does — the clever part

Step 1 of the plan looks simple but does something worth explaining, because
it is what makes the recommendations specific rather than generic.

It searches **in chained stages**, where each stage uses what the last one
found:

> **Stage 1.** Read the main Kyoto page. Ask the travel guide's own index:
> *what districts does Kyoto actually have?*
> → Arashiyama, Central, East, Higashiyama, North, South
>
> **Stage 2.** Given this traveller wants quiet and traditional, which of
> those six fit? → Higashiyama and Arashiyama. Now go and read **those two
> specific pages**.
>
> **Stage 3.** Within those two neighbourhoods only, search for vegetarian
> food.
>
> **Stage 4.** Is anything important still missing? If yes, one more targeted
> search. If no, stop.

The important thing: **Stage 2 could not have happened without Stage 1.** The
system did not guess that Higashiyama exists — it looked up the real list of
districts and chose from it. That is why this counts as genuine multi-stage
research rather than three searches in a row.

It also stops sensibly. There is a hard limit on stages, and it stops early
once it has enough. Systems like this can otherwise search forever, which is
slow and costs money.

### When a tool breaks

External services go down. When one does, **the run does not fail.** The tool
reports back in plain terms:

> *Weather data could not be retrieved. Continue planning without weather
> advice, tell the user forecasts were unavailable, and do not call this tool
> again for this location.*

That last sentence matters more than it looks. Without it, systems like this
tend to either try the broken tool over and over, or invent the missing
information. Being told explicitly what to do instead prevents both.

A broken weather service costs you the forecast. It does not cost you the
itinerary.

---

## Step 7 — Is the plan still right?

After each step, the system reviews progress and picks one of three options:

- **Carry on** — the plan is working.
- **Change the plan** — something failed, or a finding changed the picture.
  Only the *remaining* steps are rewritten; finished work is left alone.
- **Stop early** — enough has been gathered to answer well.

**It leans towards stopping early**, on purpose. Plans are written before
anything is known, so they routinely include steps that turn out to be
unnecessary. And anything asked "could this be better?" always says yes. Left
unchecked, the system would keep researching long past the point of usefulness
while someone waits.

There is also a hard limit on how many times the plan can be rewritten. If it
runs out, the system answers with what it has and says the research was cut
short — **a partial answer that arrives beats a perfect one that never does.**

---

## Step 8 — Write the answer

Now everything gathered gets turned into the reply. Three rules apply.

**Stick to what was actually found.** Recommendations must come from the
research. Inventing a restaurant is the worst thing this system could do,
because it is invisible — a made-up restaurant reads exactly like a real one
until someone turns up and it isn't there. Afterwards, an automatic check
scans the reply for named places and prices that didn't appear anywhere in the
research and flags them for review.

**Be honest about gaps.** If a service was down, say so. If the flight prices
are simulated — which they are, and the reply says so — never imply they can
be booked.

**Reply in their language.** If they wrote in Japanese, the whole reply is in
Japanese.

There is a subtlety here worth calling out. The *research* is always done in
English, because the travel guides are richest in English. Only the final
write-up switches language. So someone asking in Japanese gets the full depth
of the English guides, rather than whatever happens to have been translated.
Place names are given in local script with a romanised version in brackets, so
they can be typed into a map.

**Result:** a two-day itinerary, organised by day and by morning / afternoon /
evening, grouped so they aren't crossing the city repeatedly, with vegetarian
places named, and quieter alternatives to the busiest temples — because it
remembered they dislike crowds.

---

## Step 9 — Learn something, afterwards

**The reply has already been sent.** This step happens in the background, so
nobody waits for it.

The system looks at what the person said and asks: *is there anything here
worth remembering for a completely different trip in two years' time?*

From our example: **"I'm vegetarian"** — yes. That is true regardless of
destination.

Things it deliberately does **not** store:
- *"Going to Kyoto"* — useless once the trip is over
- *"I'm tired today"* — temporary
- Questions. Asking *"are there vegan restaurants?"* does not make someone
  vegan
- Names, emails, phone numbers, payment details — never

### The bit that keeps memory useful

Before saving anything, it checks whether it already knows this.

- **Already knows it?** Don't save a second copy — just note that it has now
  been said twice. Say you're vegetarian in five different conversations and
  the system holds **one** note with a count of five, not five identical notes.
- **Contradicts something it knows?** *"I'm happy to splurge on hotels"* when
  it previously recorded *"travels on a tight budget"* — the old note is
  retired and marked as replaced. It isn't deleted, so anyone reviewing can
  see how the person's preferences changed.
- **Genuinely new?** Save it.

Without this, memory rots. A system that saves every mention ends up with
dozens of near-identical notes crowding out everything else — and eventually
gives worse answers than one with no memory at all.

**Result:** one new note — *"Traveller is vegetarian"* — marked as a hard
requirement, so it will be loaded on every future trip to anywhere.

---

## What happens next time

They come back a month later and type:

> *3 days in Lisbon*

Different city, different trip, new conversation. But:

- The vegetarian requirement is loaded automatically
- So is the note about disliking crowds
- The system does **not** ask about either
- Restaurant searches are filtered to vegetarian **at source** — not found and
  then filtered, but requested that way

They never repeated themselves. That is the whole point.

---

## Seeing it for yourself

Three ways to check any of the above actually happens, rather than taking this
document's word for it.

**What does it remember about me?**
`GET /api/v1/me/memories` — every stored fact, how confident it is, and how
many times you've mentioned it.

**What did it actually do?**
Every reply includes a `run_id`. An administrator can look up the full trace:
the plan it wrote, every step, every tool call with the exact arguments, how
long each took, and what failed. This is the best evidence that the planning
and tool-choosing described here are real.

**Is tool choice really dynamic?**
The admin dashboard shows call counts per tool. Different questions produce
visibly different patterns — which would not happen if the tools were fired in
a fixed order.

---

## Roughly how long it takes

| Step | Time |
|---|---|
| 1–2 — Identity and memory | under 1s |
| 3–4 — Understanding | 1–2s |
| 5 — Planning | 1–2s |
| 6 — Carrying out the plan | 5–15s (most of the total) |
| 7 — Reviewing | under 1s per step |
| 8 — Writing the answer | 2–4s |
| **Total** | **10–20s** |
| 9 — Learning | after the reply; nobody waits |

The first request after a quiet period is slower — the free hosting puts the
service to sleep after 15 minutes and takes half a minute to wake up. A city
nobody has asked about before is also slower the first time, because its guide
pages have to be fetched and processed; the second person to ask about that
city gets a much faster answer.
