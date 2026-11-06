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
   - confirms, per Brightspace course, that the syllabus allows AI study tools
   - picks **multiple choice**, **fill in the blank**, or a **mix**, and a starting deck size
2. **Players** choose **Join a game**, type the 6-character code and a first name.
3. Everyone can **add material**: drop PDF, Word, PowerPoint, text or HTML files, or paste notes,
   tagged to a section. Players connect Brightspace so the game can confirm they're enrolled.
4. The host presses **Build deck**. The server reads the selected Brightspace modules, writes
   questions per section, and **deals them in rotation across sections** (stratified), so every
   module and topic is covered evenly.
5. The host **draws** each card. Every player answers on their own phone; answers are checked
   (multiple choice exactly; fill in the blank allowing case, articles, accepted alternatives
   and small typos), and the scoreboard updates. The board itself handles movement.
6. When the deck drops to about a quarter of its size, new cards are written in the background
   from the next passages of each section, never repeating earlier questions.
7. **End game** deletes all material, questions and scores.

## Run it

```bash
pip install -e ".[web]"
```

```bash
study-deck
```

Then open <http://127.0.0.1:8000>. To write real questions, open **Settings** (top right),
paste your Purdue GenAI Studio API key, press **Check key**, pick a model and **Save**. When the
app runs on your own computer you can tick **Remember on this computer**: the key is then kept
in your system keychain (macOS Keychain, Windows Credential Manager), never in a file, and is
only used for requests from that computer. Without further settings it runs in **demo mode**: two
sample courses and a simple offline question writer, so the whole game can be tried with no
Purdue access. Pasting a GenAI Studio API key when creating a game switches that game to
Purdue GenAI Studio.

### Settings (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `STUDY_DECK_OAUTH_CLIENT_ID` | – | Purdue-issued Brightspace OAuth 2.0 client ID. When set, demo mode turns off and players sign in to real Brightspace. |
| `STUDY_DECK_OAUTH_CLIENT_SECRET` | – | Only if the registered app is confidential. |
| `STUDY_DECK_PUBLIC_URL` | `http://127.0.0.1:8000` | Public base URL. The registered redirect URI must be `<PUBLIC_URL>/auth/brightspace/callback`. Use `https://` in production (cookies are then Secure-only). |
| `STUDY_DECK_SESSION_SECRET` | random per start | Signs the browser cookie. Set a long random value in production. |
| `STUDY_DECK_GENAI_URL` | `https://genai.rcac.purdue.edu/api/chat/completions` | Must be an `https` URL on a `purdue.edu` host; anything else is refused. |
| `STUDY_DECK_GENAI_MODEL` | `llama3.1:latest` | A model available in your GenAI Studio account. |
| `STUDY_DECK_GENAI_API_KEY` | – | Server-wide GenAI Studio key. Without it, each host pastes their own key (held in memory for that game only). |
| `STUDY_DECK_DEMO` | on when no client ID | Force demo mode on or off. |
| `STUDY_DECK_SESSION_TTL_MINUTES` | `240` | Idle games and sign-ins are deleted after this. |
| `STUDY_DECK_MAX_PLAYERS` | `8` | Players per game. |

The registered Brightspace app needs these read-only scopes:

```
calendar:my_events:read content:file:read content:toc:read dropbox:folders:read enrollment:own_enrollment:read grades:own_grades:read news:newsitems:read
```

(Study Deck itself uses `enrollment:own_enrollment:read`, `content:toc:read` and
`content:file:read`; the rest serve the MCP server if both share one registration.)

## Architecture

```
Browser (vanilla JS, polls every 1.5 s)
   |  JSON API, signed session cookie, X-Study-Deck header on every POST
Starlette app (study_deck/app.py)
   |-- game.py        sessions, sharing rule, stratified deck, top-ups, answer checking (all in memory)
   |-- brightspace.py OAuth 2.0 + PKCE per player; read-only allowlisted client with course-file reading
   |-- extract.py     PDF / Word / PowerPoint / HTML / text -> redacted text -> passages; topic relevance
   '-- genai.py       Purdue GenAI Studio chat completions; JSON validation; offline demo writer
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
