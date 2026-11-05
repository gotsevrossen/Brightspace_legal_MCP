# Purdue Brightspace MCP (policy-aware)

A read-only [Model Context Protocol](https://modelcontextprotocol.io) server that lets an AI
assistant see a Purdue student's **own** Brightspace data: courses, due dates, assignments,
announcements, submission status and grades. It is built to follow Purdue's IT, data
classification, FERPA, intellectual property, academic integrity and AI-use policies.

This is a from-scratch redesign of
[pranav-vijayananth/brightspace-mcp-server](https://github.com/pranav-vijayananth/brightspace-mcp-server).
**[POLICY_COMPLIANCE.md](POLICY_COMPLIANCE.md)** lists every Purdue policy considered, what the
original did that conflicts with them, and how this version handles each one.

> Unofficial. Not affiliated with or approved by Purdue University or D2L, and not legal advice.

## What is different from the original

| | Original | This server |
|---|---|---|
| Purdue password | Plaintext `.env`, typed by a bot | Never seen. You sign in on Purdue's page yourself (S-13, S-16). |
| How data is read | HTML scraping with CSS selectors | Documented Brightspace REST API, GET only, fixed route allowlist |
| Auth | Automated login | OAuth 2.0 + PKCE (default), or opt-in manual browser session held in memory |
| Data sent to the AI | Everything, unlabelled | Gated by Purdue data tier (Public / Sensitive / Restricted), every result labelled (VII.A.5) |
| Course materials | No limits | Instructor text only for courses whose syllabus you confirmed allows AI (I.A.1, academic integrity) |
| Other students' data, quizzes, files | Not prevented | Blocked before any request (FERPA, academic integrity) |
| Files on disk | JSON dump and screenshots | None, apart from a metadata-only audit log |
| Load | Unlimited | 1 request/s at most, hourly cap, cache (VII.A.4) |

## Tools

| Tool | Data tier | Notes |
|---|---|---|
| `compliance_status` | Public | Shows the settings in force and why. No Brightspace request. |
| `sign_in` / `sign_out` | – | Opens Purdue's sign-in page / ends the session and clears tokens. |
| `list_courses` | Sensitive | Your enrollments and their course IDs. |
| `upcoming_due_dates` | Sensitive | Calendar events for your active courses, up to 60 days ahead. |
| `list_assignments` | Sensitive | Names, due dates, points. Instructions only for AI-permitted courses. |
| `assignment_submission_status` | Sensitive / Restricted | Submission dates. Score and feedback only at the Restricted tier. |
| `get_announcements` | Sensitive | Titles and dates. Bodies only for AI-permitted courses. |
| `get_course_outline` | Sensitive | Module and topic titles. No file contents. |
| `get_my_grades` | Restricted | Your released grades. |

There are deliberately **no** tools for quizzes or exams, classlists, groups, discussions, email,
file downloads, or anything that writes to Brightspace.

## Install

Requires Python 3.10+.

```bash
git clone https://github.com/gotsevrossen/Brightspace_legal_MCP.git
```

```bash
cd Brightspace_legal_MCP && python3 -m venv .venv && . .venv/bin/activate
```

```bash
pip install -e .
```

For browser mode, also install Playwright:

```bash
pip install -e ".[browser]" && playwright install chromium
```

## Configure

All settings are environment variables, set in your MCP client's config. Note that there is no
password setting.

| Variable | Default | Meaning |
|---|---|---|
| `PURDUE_MCP_AI_DATA_TIER` | `public` | Highest Purdue data tier **your AI client is authorized for** on Purdue's AI tools list. `public` disables every data tool. `sensitive` enables everything but grades and feedback. `restricted` enables those too. |
| `PURDUE_MCP_AI_PERMITTED_COURSES` | *(none)* | Comma-separated course IDs whose syllabus you checked and which allow AI with course materials. `*` is rejected. |
| `PURDUE_MCP_AUTH_MODE` | `oauth` | `oauth` or `browser`. |
| `PURDUE_MCP_OAUTH_CLIENT_ID` | – | Issued by Purdue's Brightspace admins for an approved OAuth 2.0 app. |
| `PURDUE_MCP_OAUTH_CLIENT_SECRET` | – | Only if the registered app is confidential. Prefer the OS keyring or your client's secret store. |
| `PURDUE_MCP_OAUTH_REDIRECT_PORT` | `8765` | The registered redirect URI must be `http://127.0.0.1:<port>/callback`. |
| `PURDUE_MCP_BROWSER_MODE_ACKNOWLEDGED` | `false` | Must be `true` to use browser mode. Read POLICY_COMPLIANCE.md §4 first. |
| `PURDUE_MCP_MAX_REQUESTS_PER_HOUR` | `300` | Capped at 600. |
| `PURDUE_MCP_AUDIT_LOG` | `true` | Metadata-only log at `~/.local/state/purdue-brightspace-mcp/audit.jsonl`. |

### Choosing the data tier honestly

Purdue's AI Use policy (VII.A.5) and its authorized AI tools list say:

- An AI tool **not** on the list, including a personal Claude, ChatGPT or Gemini account, may
  receive **Public data only**. Keep `PURDUE_MCP_AI_DATA_TIER=public`.
- Sensitive or Restricted data may go only to a tool authorized for that tier, **accessed through
  Purdue SSO** under Purdue's enterprise or API license. Check the
  [current list](https://www.purdue.edu/ai/enterprise-ai-toolkit/) before raising the tier.

### Example: Claude Desktop

```json
{
  "mcpServers": {
    "purdue-brightspace": {
      "command": "/absolute/path/to/Brightspace_legal_MCP/.venv/bin/purdue-brightspace-mcp",
      "env": {
        "PURDUE_MCP_AI_DATA_TIER": "sensitive",
        "PURDUE_MCP_AI_PERMITTED_COURSES": "123456",
        "PURDUE_MCP_AUTH_MODE": "oauth",
        "PURDUE_MCP_OAUTH_CLIENT_ID": "issued-by-purdue"
      }
    }
  }
}
```

Use these values only if that Claude Desktop is signed in through Purdue's enterprise license.
Then ask: *"Check my Purdue compliance status"*, *"Sign me in to Brightspace"*, *"What's due in
the next two weeks?"*

## Getting an OAuth client ID

Students cannot register Brightspace API apps. The sanctioned route is through Purdue: an
instructor, department or student organization files a
[New Brightspace Tool Integration](https://www.purdue.edu/innovativelearning/tools-resources/instructional-technology/new-brightspace-tool-integration)
request, or Purdue IT registers the app under *Manage Extensibility > OAuth 2.0*. Use the
authorization-code grant, redirect URI `http://127.0.0.1:8765/callback`, and these read-only
scopes:

```
calendar:my_events:read content:toc:read dropbox:folders:read enrollment:own_enrollment:read grades:own_grades:read news:newsitems:read
```

## Development

```bash
pip install -e ".[dev]" && pytest
```

The tests check the policy gates directly. Examples: every data tool refuses at the default
tier, grades need Restricted, instructor text is withheld without course permission, forbidden
routes are blocked before any request, no credential settings exist, and the audit log holds
no content.

## License

Apache-2.0. See [LICENSE](LICENSE).
