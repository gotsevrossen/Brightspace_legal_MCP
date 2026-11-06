# Purdue policy review for a Brightspace MCP connector

This document lists the Purdue University policies that apply to an MCP connector that reads a
student's Brightspace data and hands it to an AI assistant. It explains how each one shaped this
server, and where the original project
([pranav-vijayananth/brightspace-mcp-server](https://github.com/pranav-vijayananth/brightspace-mcp-server))
falls short of it.

> **Not legal advice, and not a Purdue approval.** This is a good-faith reading of public policy
> pages as of November 2026. The purdue.edu policy pages could not be loaded directly while this
> was written, so the policy wording below comes from search-indexed excerpts of the official
> pages. Check the current text at the links below. For a binding answer, ask Purdue IT
> (itpolicyanswers@purdue.edu), the Registrar for FERPA (ferpa@purdue.edu), or your instructor.

## 1. Policies that apply

| # | Policy | What it says that matters here |
|---|--------|--------------------------------|
| P1 | **Acceptable Use of IT Resources and Information Assets (VII.A.4)** | Covers all users of Purdue IT resources, Brightspace included. Use is limited to University-related purposes. Defines **Sensitive Data** ("access must be guarded due to proprietary, ethical or privacy considerations") and **Restricted Data** ("access is restricted under applicable legal, regulatory or policy requirements"). Requires blocking unauthorized access and unauthorized use. |
| P2 | **Artificial Intelligence Use (VII.A.5)**, issued Nov 1, 2025, reviewed Feb 10, 2026 | Applies to all Purdue people who use AI. Sensitive and Restricted data **must not be entered into AI tools without prior approval**. The tool must be on the authorized list for that tier, and some uses need Data Ethics Committee review. AI must not be the sole basis for decisions about people. |
| P3 | **Purdue Authorized AI Tools list** (AI@Purdue, latest Oct 2026) and *Application of the AI Use Policy* (Secure Purdue) | Tools not on the list are authorized for **Public data only**. With Sensitive or Restricted data, the tool **must be accessed through Purdue SSO** or an equivalent. OpenAI, Microsoft, Google and Anthropic models need an **Enterprise or API-level license**. AI outputs from such use must carry a sensitivity label. Anthropic/Claude appears in the enterprise section through Purdue's licensing (a pilot in 2026). A personal Claude account is not listed. |
| P4 | **Data Classification and Handling Procedures** (Secure Purdue) | Every University dataset is Public, Sensitive or Restricted. Purdue's examples: course offerings are Public; student IDs and course rosters are Sensitive; **grades are Restricted**. Sensitive and Restricted files go in Purdue-authenticated storage. |
| P5 | **Authentication, Authorization and Access Controls standard (S-13)** | "Authentication credentials for Purdue IT Resources will not be coded into programs or queries unless they are encrypted, and only when no other reasonable options exist." |
| P6 | **User Credentials standard (S-16)** | Passwords are used only by the authorized user. Purdue IT will never ask for them. Credentials are themselves protected data. |
| P7 | **Access to Student Education Records (VIII.A.4)** and FERPA | Education records and personally identifiable information are released only with the student's consent, except where FERPA allows. Photos are education records and are not directory information at Purdue. Classlists, classmates' posts and group rosters are other students' records. |
| P8 | **Intellectual Property (I.A.1)** | Instructional works belong to their authors, with a license to the University. Course materials (announcements, instructions, slides, files) are not the student's to redistribute. |
| P9 | **Student Regulations: academic integrity**, plus Provost / Teaching@Purdue AI guidance | Using unauthorized materials or aids in any academic exercise is prohibited. **Each instructor sets the AI rules for their course in the syllabus.** |
| P10 | **New Brightspace tool integration process** (Innovative Learning) and **New IT Solutions and Services Review** (Secure Purdue) | Third-party Brightspace integrations get a security review and approval from the Purdue System Brightspace Change Management Board. New software that handles University data gets a Systems Security review. API apps are registered by Brightspace admins (Manage Extensibility > OAuth 2.0). |

## 2. Problems in the original project

| Original behaviour | Policy | Problem |
|---|---|---|
| Puts `PURDUE_USERNAME` / `PURDUE_PASSWORD` in a plaintext `.env` file and has a bot type them into Purdue's login form | P5, P6, P1 | Plaintext credentials used by a program when another option exists (the user signing in). |
| Sends Brightspace data to Claude Desktop with no regard to data tier | P2, P3, P4 | Enrollments, assignments and (planned) grades are Sensitive or Restricted. A personal AI account is Public-only. |
| Saves `brightspace_data.json` and debug screenshots (`homepage_debug.png`, `error_getting_courses.png`) to the working directory | P4 | Unlabelled copies of Sensitive data, and screenshots that can capture Restricted data, left on local disk. |
| Scrapes HTML through CSS selectors with no rate limit or route limits | P1, P10 | Unreviewed automation against a University system, with no limit on load or scope. |
| Planned grade and assignment scraping, with no FERPA or academic-integrity guards | P7, P9 | Nothing stops classlists, classmates' content, or quiz content from reaching the AI. |
| README says it "respects Purdue's terms of service" | all | No basis is given. Nothing in the code enforces it. |

## 3. How this server implements each policy

| Control | Where | Policies |
|---|---|---|
| **No password handling at all.** There is no username or password setting. Sign-in happens on Purdue's own page, in the user's browser, with Duo approved by the user. | `config.py`, `auth/` | P5, P6 |
| **OAuth 2.0 (authorization code + PKCE) is the default** auth mode. It needs a client ID from a Purdue-approved app registration, with least-privilege **read-only scopes** (`*:read` only, no `enrollment:orgunit:read`). | `auth/oauth.py`, `config.DEFAULT_OAUTH_SCOPES` | P10, P1 |
| Refresh tokens are kept in the **OS keyring** (encrypted at rest), or in memory only. | `auth/oauth.py` | P5 |
| **Browser mode is opt-in** (`PURDUE_MCP_BROWSER_MODE_ACKNOWLEDGED=true`). The user signs in manually, and the session is kept **in memory only** (no persistent profile, cookies or screenshots). | `auth/browser.py` | P5, P6, P4 |
| **Data-tier gate.** Every tool declares Public, Sensitive or Restricted. `PURDUE_MCP_AI_DATA_TIER` defaults to `sensitive`, which assumes a Purdue-authorized AI client used through Purdue SSO. **Grades, scores and feedback (Restricted) stay refused** until the user sets `restricted`. Setting `public` turns off every data tool. | `classification.py`, `policy.py` | P2, P3, P4 |
| **Classification labels** on every result (`purdue_data_classification` plus handling text). | `sanitize.labelled` | P3, P4 |
| **Per-course instructor-permission gate.** Instructor-authored text (announcement bodies, assignment instructions, event descriptions) is released only for course IDs listed in `PURDUE_MCP_AI_PERMITTED_COURSES`, after the student has checked the syllabus. `*` is rejected. Titles and dates stay available. | `policy.py`, `tools.py` | P8, P9 |
| **Own data only.** Every per-course tool checks that the course is in the user's own enrollments. Only `my*` routes are used for grades and submissions. | `tools._require_own_course`, `client.ALLOWED_ROUTES` | P1, P7 |
| **Route allowlist, GET only.** Quizzes, surveys, classlists, groups, discussions, messages, and file and attachment downloads are refused before any request is made. There is no write code path. | `client.py` | P7, P8, P9, P1 |
| **Data minimization and redaction.** Only needed fields are copied. Other people's IDs, instructor emails and class-wide counts are dropped. Email addresses and PUID-like numbers in free text are redacted. Text is truncated. | `sanitize.py`, `tools.py` | P4, P7 |
| **Load limits.** At least 1 s between requests, at most 600 per hour (default 300), a 5-minute cache, and capped paging. Config cannot loosen the floors. | `client.RateLimiter`, `config.py` | P1 |
| **Host lock** to `https://purdue.brightspace.com` (plus D2L's OAuth endpoints). | `config.ALLOWED_HOSTS`, `client.py` | P1 |
| **Nothing written to disk** except a metadata-only audit log (`0600`, under `~/.local/state/purdue-brightspace-mcp/`), which records which tool and route ran, never content. | `policy.AuditLog` | P4, P1 |
| **Assistant instructions** tell the model to treat results as confidential, accept refusals as final, never ask for credentials, and not use course materials to complete graded work. | `server.INSTRUCTIONS` | P2, P6, P9 |

## 4. What code cannot settle (your responsibility)

1. **Which tier to declare.** The default, `sensitive`, is compliant only if the AI client you
   connect is on Purdue's current authorized list for Sensitive data **and** you use it through
   Purdue SSO under Purdue's enterprise license. A personal Claude, ChatGPT or Gemini account
   counts as Public-only: set `PURDUE_MCP_AI_DATA_TIER=public`, and then nothing but
   `compliance_status` works. Set `restricted` (grades) only for a tool authorized for Restricted
   data.
2. **Your own records.** FERPA lets students share their own records. Purdue's AI policy is
   written around University data in general, and this server takes the cautious reading: a
   student's own grades are still treated as Restricted. If Purdue IT tells you otherwise for
   your own records, that is their call to make, not this code's.
3. **Getting an OAuth client ID.** Students cannot register Brightspace API apps. The sanctioned
   route is for an instructor, department or student organization to request a New Brightspace
   Tool Integration, or for Purdue IT to register the app. Until then, browser mode is the only
   option, and it is a grey area: it uses your own session for automated read-only requests
   that Purdue has not explicitly authorized. Use it only after reading this document, or ask
   itpolicyanswers@purdue.edu first.
4. **Course AI rules.** Add a course to `PURDUE_MCP_AI_PERMITTED_COURSES` only after reading its
   syllabus. Even then, what you do with the AI's help is governed by the course's academic
   integrity rules.
5. **Policies change.** The AI tools list was revised several times in 2025 and 2026. Check it
   again each semester.

## 5. Sources

- Acceptable Use of IT Resources and Information Assets (VII.A.4): <https://www.purdue.edu/vpec/policies/information-technology/viia4/>
- Acceptable Use procedures: <https://www.purdue.edu/securepurdue/it-policies-standards/it-procedures/acceptable-use-of-resources-procedures.php>
- Artificial Intelligence Use (VII.A.5): <https://www.purdue.edu/vpec/policies/information-technology/viia5/>
- Application of the AI Use Policy: <https://www.purdue.edu/securepurdue/it-policies-standards/Application%20of%20AI.php>
- Purdue Authorized AI Tools (Oct 2026): <https://www.purdue.edu/ai/wp-content/uploads/2026/10/AITools.pdf>
- AI Tools at Purdue by provider (Sep 2026): <https://www.purdue.edu/ai/wp-content/uploads/2026/09/purdue_ai_tools.pdf>
- Compare Purdue's AI tools and licenses: <https://www.purdue.edu/ai/enterprise-ai-toolkit/>
- Data Classification and Handling Procedures: <https://www.purdue.edu/securepurdue/data-handling/index.php>
- Keys to Securing Purdue's Data: <https://purdue.edu/securepurdue/data-handling/keys-to-securing-data.php>
- Math department data examples (public / sensitive / restricted): <https://math.purdue.edu/resources/gradta/data_policies.html>
- Authentication, Authorization and Access Controls (S-13): <https://purdue.edu/policies/information-technology/s13.html>
- User Credentials (S-16): <https://purdue.edu/policies/information-technology/s16.html>
- Access to Student Education Records (VIII.A.4): <https://purdue.edu/policies/records/viiia4.html>
- FERPA basics for students: <https://www.purdue.edu/registrar/currentStudents/FERPA%20Basics%20for%20Students.html>
- Intellectual Property (I.A.1): <https://purdue.edu/policies/academic-research-affairs/ia1.html>
- Student conduct code: <https://purdue.edu/odos/osrr/conduct/code.html>
- Guidance on the Use of AI in Teaching and Learning: <https://purdue.edu/teaching-learning/instructors/ai.php>
- New Brightspace tool integration: <https://www.purdue.edu/innovativelearning/tools-resources/instructional-technology/new-brightspace-tool-integration>
- New IT Solutions and Services Review: <https://purdue.edu/securepurdue/services/solution-services-review.php>
- D2L OAuth 2.0 and scopes table: <https://docs.valence.desire2learn.com/basic/oauth2.html>, <https://docs.valence.desire2learn.com/http-scopestable.html>
