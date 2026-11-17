# Study Deck

A web app for a physical study board game. Players pick what they're studying, pool material
from Purdue Brightspace and their own notes, and the app writes a deck of short questions
(multiple choice or fill in the blank) through **Purdue GenAI Studio**. The deck tops itself up
automatically when it runs low. It is built on the same policy controls as the Brightspace MCP
server in this repository; see [POLICY_COMPLIANCE.md](POLICY_COMPLIANCE.md) §5.

> Unofficial. Not affiliated with or approved by Purdue University or D2L. Running this for other
> students needs Purdue's approval first (see "Before real students use it").

## How a game works

1. **Host** opens the site and chooses **Host a game**:
   - picks one or more courses: Brightspace courses, or a course typed in by hand for uploaded material only
   - for each course, studies the **whole course**, chosen **modules**, or **topics**
     (comma-separated). Every module and every topic becomes its own *section* of the deck.
     Folders inside folders are listed too (indented), so a course that keeps everything in one
     folder can still be split by the folders inside it.
   - confirms, per Brightspace course, that the syllabus allows AI study tools
   - picks **multiple choice**, **fill in the blank**, or a **mix**, and a starting deck size (any number, 1 up to `STUDY_DECK_MAX_DECK_SIZE`)
   - when a chosen course is on Boilerexams, leaves **Use past exam questions** on (see
     "Past exam questions" below) or turns it off
2. **Players** scan the QR code in the lobby with their phone camera (or open the address shown
   under it and type the 6-character code), then enter a first name.
3. Everyone can **add material**: drop PDF, Word, PowerPoint, text or HTML files (or whole
   folders, read at any depth), or paste notes, tagged to a section. Each drop or pick adds to
   the list; nothing is sent until **Add to the deck**. Up to 60 files per player. Players connect
   Brightspace so the game can confirm they're enrolled.
4. The host presses **Build deck**. The server reads the selected Brightspace modules, writes
   questions per section, and **deals them evenly across sections** (stratified) in a random order, so every
   module and topic is covered evenly.
   A deck reads at most 30 files per course. When a section is a
   typed topic, or the chosen folders hold more files than that, the AI is shown an outline of
   folder and file **titles only** (no file contents) and picks which folders to open, at any
   depth, and which single files to read. Without an AI that can pick, titles are matched against
   the topics instead. The lobby lists which files were read and how they were chosen.
5. The host **draws** each card. The people playing the physical board game decide who answers.
   **Only the host's screen can enter answers**: it shows the options or an answer box,
   and *Didn't know*. Answers
   are checked (multiple choice exactly; fill in the blank allowing case, articles, accepted
   alternatives and small typos), and every screen shows the result without assigning it to
   a player. Phones show the card but have no answer controls. Turns, scores and movement
   are handled by the physical board game.
   The host can **remove a player** (× next to their name). They can't rejoin that game, and
   their uploaded material leaves with them.
6. When 5 cards are left in the deck, 10 new cards are written in the background (shuffled in too)
   from the next passages of each section, never repeating earlier questions.
7. **End game** deletes all material, questions and scores.

## Past exam questions (Boilerexams)

### Study without Brightspace or uploads

On **Host a game**, type a course code such as **PHYS 17200** into the normal
course field and click **Add course**. Matching courses are marked **On Boilerexams**.
Select the whole course or enter topics, leave
**Use past exam questions** checked, create the game, then press **Build deck**.
No Brightspace sign-in or uploads are needed. A Purdue GenAI Studio key is still required.

When a section has no other material, matching past exam questions and their correct
answers become the AI's source material. Multiple-choice or mixed decks include actual
past-exam cards plus new questions labeled **AI practice · based on Boilerexams**.
Fill-in-the-blank-only decks contain AI practice questions. Top-ups use the same source
and avoid repeating cards. If you later add notes or files, those become the AI's material.
If the course or topic has no usable questions, add material or choose another selection;
the app does not substitute questions from unrelated topics.

### Supplementing your own material

[Boilerexams](https://boilerexams.com) publishes Purdue past exams that professors have
released, for courses such as MA 16100, MA 26100, CS 25100, CHM 11510, PHYS 17200 and STAT 35000.
When a game's course is one of them (matched by the course number in its name, so
"MA 16100", "MA 161" and "Fall 2026 MA 16100-LEC" all match), Study Deck uses that course's
past exam questions in two ways:

- **As cards:** about a third of each matching section's cards are real past exam questions,
  only from the Boilerexams topics that fit the section (its modules or typed topics; any
  topic for a whole-course section). A section whose topics don't fit any gets none. These
  cards are labeled **Past exam · Fall 2018** (the exam they came from) and their answer
  credits Boilerexams. Multiple choice only, so fill-in-the-blank-only games don't get them.
  They're never repeated, and top-ups add new ones.
- **As a style guide when other material is available:** up to six are shown to the AI as examples of how the course's exam
  questions are written (phrasing, what they give and ask for, difficulty, how the options
  are worded). The cards the AI writes still take every fact, answer and explanation from
  the game's own material.

Details:

- **Text only:** questions that need a picture (a graph, a drawn structure, "shown below",
  options that are just letters, arrays or tables) are left out. Math is written out with
  symbols (√, ², ₂, π, →, fractions as a/b); a question whose math can't be written out
  readably is used only as a style example, never as a card.
- **Only mentioned when used:** the host setup offers it only when a chosen course is on
  Boilerexams, and the game shows a Boilerexams note only when it actually used some. If
  Boilerexams can't be reached, or has nothing that fits, the deck is built as usual and
  nothing is said.
- **Light on their server:** questions are read one at a time with a short pause, only as many
  as needed, and cached in memory for six hours. Requests are plain public reads with nothing
  about the players in them.
- **Easy to turn off:** untick it per game in the host setup, or set
  `STUDY_DECK_BOILEREXAMS=false` to turn it off for the whole server.

Boilerexams has no public API: this reads the same data its website loads, so it can stop
working if they change their site. Boilerexams is unaffiliated with Study Deck, and its
questions belong to the professors who released them. Ask the Boilerexams team
(`Boilerexams@gmail.com`) before offering this to other students.

## Run it

Study Deck runs on one computer (usually the host's laptop) and everyone else plays on their
phone or laptop over the same Wi-Fi.

**Windows:** double-click **Start Study Deck.bat**. The first run sets everything up (needs
Python 3.10 or newer), then Study Deck opens in your browser. The window shows two addresses:

```
Study Deck is running.
  On this computer:   http://127.0.0.1:8000
  Same Wi-Fi players: http://192.168.1.23:8000
```

**Any system:**

```bash
pip install -e ".[web]"
```

```bash
study-deck
```

Before the first game, open **Settings** (top right), paste your Purdue GenAI Studio API key,
press **Check key**, pick a model and **Save**. Tick **Remember on this computer** to keep the
key in your system keychain (macOS Keychain, Windows Credential Manager), never in a file. A
game can't be created without a key: every card is written by Purdue GenAI Studio.

### Playing with other people

- Everyone must be on the **same Wi-Fi** as the computer running Study Deck. Once a game is
  created, the lobby shows a **QR code** and the address; players scan it and type their name.
- **Windows asks once** whether Python may use the network. Allow it on **Private networks**.
  If phones can't connect, check that Windows lists your Wi-Fi as a *Private* network
  (Settings → Network & internet → Wi-Fi → your network); Public networks block incoming
  connections. Campus Wi-Fi such as eduroam usually blocks device-to-device traffic, so use a
  home network or a phone hotspot there.
- Keep the computer awake and the Study Deck window open for the whole game. Stopping it ends
  every game.
- **Connect Brightspace** (personal sign-in, on in the Windows launcher): a course's Brightspace
  files are only used once every player is confirmed as enrolled. Players on phones tap
  **Connect Brightspace** on the game page, then walk over to the host's computer, where a
  Purdue sign-in window with their name has opened, and sign in there.
- To keep Study Deck to this computer only, set `STUDY_DECK_HOST=127.0.0.1`.

### Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `STUDY_DECK_HOST` | `0.0.0.0` | Network address to listen on. `0.0.0.0` lets players on the same Wi-Fi join; `127.0.0.1` keeps it to this computer. |
| `STUDY_DECK_PORT` (or `PORT`) | `8000` | Port to listen on. |
| `STUDY_DECK_OPEN_BROWSER` | `true` | Open Study Deck in this computer's browser on start. |
| `STUDY_DECK_OAUTH_CLIENT_ID` | – | Purdue-issued Brightspace OAuth 2.0 client ID. When set, every player signs in to Brightspace on Purdue's page. |
| `STUDY_DECK_OAUTH_CLIENT_SECRET` | – | Only if the registered app is confidential. |
| `STUDY_DECK_PUBLIC_URL` | `http://127.0.0.1:<port>` | Public base URL. The registered redirect URI must be `<PUBLIC_URL>/auth/brightspace/callback`. Use `https://` in production (cookies are then Secure-only). |
| `STUDY_DECK_SESSION_SECRET` | random per start | Signs the browser cookie. Set a long random value in production. |
| `STUDY_DECK_GENAI_URL` | `https://genai.rcac.purdue.edu/api/chat/completions` | Must be an `https` URL on a `purdue.edu` host; anything else is refused. |
| `STUDY_DECK_GENAI_MODEL` | `llama3.1:latest` | A model available in your GenAI Studio account. |
| `STUDY_DECK_GENAI_API_KEY` | – | Server-wide GenAI Studio key. Without it, each host pastes their own key (held in memory for that game only). |
| `STUDY_DECK_BROWSER_MODE_ACKNOWLEDGED` | `false` | Personal sign-in (see below). Ignored when a client ID is set. |
| `STUDY_DECK_BOILEREXAMS` | `true` | Use Boilerexams past exam questions as cards and as a style guide for the AI (see "Past exam questions"). |
| `STUDY_DECK_SESSION_TTL_MINUTES` | `240` | Idle games and sign-ins are deleted after this. |
| `STUDY_DECK_MAX_PLAYERS` | `8` | Players per game. |
| `STUDY_DECK_MAX_DECK_SIZE` | `100` | Largest starting deck a host can choose (capped at 300; every 8 cards is one GenAI Studio request). |

### Personal sign-in (your own courses, without a Purdue client ID)

For one student running Study Deck on their own computer. Install the browser extra and turn it on:

```bash
pip install -e ".[web,browser]"
```

```bash
STUDY_DECK_BROWSER_MODE_ACKNOWLEDGED=true study-deck
```

On Windows, **Start Study Deck.bat** turns this on. To turn it off, put `rem ` in front of
its `STUDY_DECK_BROWSER_MODE_ACKNOWLEDGED` line.

Study Deck then opens in its own window of your default browser (Google Chrome or Microsoft
Edge; it's a separate window the app controls, not your usual one). **Connect Brightspace**
opens Purdue's own sign-in page as a new tab in that window. You sign in and approve the
Microsoft Authenticator prompt yourself; the app never sees your password. Once you're signed
in the tab closes by itself and you're back on Study Deck. (Clicking Connect Brightspace in
your usual browser on this computer works too: the sign-in tab opens in the Study Deck window,
and once you're signed in you can keep playing in your usual browser as long as that window
stays open.) After that it reads your enrollments, module lists and module files through the
same read-only, allowlisted, rate-limited routes as OAuth mode. The session lives in memory only
and ends when you disconnect, close the Study Deck window, or stop the app.

Limits, on purpose:

- It uses your own interactive session for automated requests, which Purdue has not explicitly
  authorized. That is why it has to be acknowledged; read [POLICY_COMPLIANCE.md](POLICY_COMPLIANCE.md) §4 first.
- It only runs while `STUDY_DECK_PUBLIC_URL` is `127.0.0.1` or `localhost`.
- Other players sign in **on the computer running Study Deck**, since that's where the browser
  is: once they've joined a game, **Connect Brightspace** on their phone opens a private window
  of that browser on the host's screen, starting on a page with their name. They sign in there
  with their own account and Authenticator. Each player's window has its own cookies, so no one's
  session is shared, and it closes when they disconnect, go idle, or the app stops. Once every
  player is verified, the course's Brightspace material goes into the deck.

The registered Brightspace app needs these read-only scopes:

```
calendar:my_events:read content:file:read content:toc:read dropbox:folders:read enrollment:own_enrollment:read grades:own_grades:read news:newsitems:read
```

(Study Deck itself uses `enrollment:own_enrollment:read`, `content:toc:read` and
`content:file:read`; the rest serve the MCP server if both share one registration.)

## Architecture

```
Browsers on the same Wi-Fi (vanilla JS, polls every 1.5 s; QR code to join)
   |  JSON API, signed session cookie, X-Study-Deck header on every POST
Starlette app (study_deck/app.py)
   |-- game.py        sessions, sharing rule, stratified deck, top-ups, answer checking (all in memory)
   |-- brightspace.py OAuth 2.0 + PKCE per player; read-only allowlisted client with course-file reading
   |-- extract.py     PDF / Word / PowerPoint / HTML / text -> redacted text -> passages; topic relevance
   |-- genai.py       Purdue GenAI Studio chat completions; JSON validation
   '-- boilerexams.py past exam questions from Boilerexams: cards and a style guide for the AI (cached)
```

## Policy design (summary)

| Rule | How it's enforced |
|---|---|
| Course materials are Sensitive data; AI tools must be authorized for them (VII.A.5) | Questions are written only by Purdue GenAI Studio; the AI endpoint is locked to `*.purdue.edu`. Only course text goes to the model: no names, grades or enrollment data. |
| FERPA: GenAI Studio excludes FERPA data | No student records are read for the game: no grades, submissions, classlists or discussion posts. Enrollment is checked inside the server and never sent to the AI. |
| Course materials belong to instructors (I.A.1) | Brightspace material from a course is used only if **every player in the game** has verified enrollment in that course through their own sign-in. Players who join after the deck is built must verify first. |
| Instructors set AI rules per course | The host must confirm each Brightspace course's syllabus allows AI study tools before the game can be created. |
| Academic integrity | Topics titled quiz, exam, midterm, answer key, solutions or graded are skipped automatically; uploaders confirm they're not adding assessments; answers never reach Brightspace. |
| No passwords (S-13, S-16) | Brightspace sign-in happens on Purdue's page (OAuth 2.0 + PKCE). Tokens live in server memory only. |
| Data handling | Nothing is written to disk except a metadata-only audit log. Games and sign-ins are deleted when the host ends the game or after the idle limit. Email addresses and PUID-like numbers are removed from all text. |
| Load on Brightspace (VII.A.4) | Same rate limiter as the MCP server (1 request/second, hourly cap), at most 30 course files per course, 15 MB per file. |
| Web security | Strict Content-Security-Policy, no inline scripts, all server text escaped, signed SameSite cookies, a custom header required on every POST, per-user request limits, opaque course keys instead of Brightspace IDs. |

## Before real students use it

A hosted service that reads several students' Brightspace content is a new Purdue integration
handling Sensitive data. Before anyone but you uses it:

1. Purdue registers the Brightspace OAuth app, after the **New Brightspace Tool Integration** review.
2. Purdue Systems Security reviews it (**New IT Solutions and Services Review**), including where it's hosted.
3. RCAC confirms that using the GenAI Studio API this way is covered by its Sensitive-data approval.
4. Purdue Trademarks & Licensing, if the game uses Purdue's name or marks; and advance approval
   under VII.A.4 if the game will be sold.
5. Meet WCAG 2.1 AA if Purdue offers it to students (the UI is built for it: real buttons and
   labels, keyboard support, 4.5:1 contrast, reduced-motion support, 44 px+ touch targets).
