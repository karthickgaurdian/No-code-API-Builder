# No-Code API Builder

[![Python](https://img.shields.io/badge/Python-3.7+-blue.svg)](https://python.org)
[![Flask](https://img.shields.io/badge/Flask-2.0+-green.svg)](https://flask.palletsprojects.com)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

A pure Python + Flask application that lets you create, test, and deploy REST APIs through a visual interface — without writing any code.

Build complex API workflows using drag-and-drop blocks, configure inputs/outputs, and deploy immediately.

---

# ✨ Features

- **No Code Required** — Build APIs visually with a drag-and-drop block interface
- **Real-time Testing** — Test APIs instantly with detailed execution traces
- **Multiple Authentication Methods** — API Key, Bearer Token, or no auth
- **Input Validation** — Automatic request validation with type checking
- **External API Integration** — Call any REST API from your workflow
- **Data Transformation** — Map, filter, and reshape data between services
- **Execution Logging** — Built-in logging for debugging and monitoring
- **Persistent Storage** — All APIs auto-saved to disk
- **Pure Python** — No external databases or services required

---

# 🚀 Quick Start

## Installation

```bash
# Save the script as api_builder.py
python3 api_builder.py

# Flask auto-installs if missing
# Open browser to:
http://localhost:5000

# API Studio — Documentation

A visual API builder for creating, testing, and deploying logic-driven endpoints — no backend code required.

---

## Getting Started

### Step 1: Create an API
Click the **+ New API** button, enter a name and route, then save.

### Step 2: Add Logic
Go to the **Flow Builder** tab, click a block to add it, and configure it.

### Step 3: Test
Go to the **Test** tab, enter a JSON payload, and click **Run Test**.

---

## Variable System

Access data anywhere using `{{source.path}}` syntax.

| Syntax | What it gets |
|---|---|
| `{{payload.name}}` | Request body field |
| `{{vars.result}}` | Output from a previous block |
| `{{query.page}}` | URL parameter |
| `{{headers.Authorization}}` | Request header |

---

## Available Blocks

| Block | What it does |
|---|---|
| **Set Variable** | Store a value |
| **Condition** | Compare values → true/false |
| **Stop If** | Halt execution when a condition is met |
| **HTTP Call** | Call an external API |
| **Transform** | Build or reshape objects |
| **Filter List** | Filter array items |
| **Hash** | Generate SHA256/MD5 |
| **Generate ID** | UUID or timestamp |
| **Format String** | Build text with variables |
| **Set Return Value** | Define the API response |

---

## Common Examples

### Example 1: Simple Greeting API

**Input:** `{"name": "Alice"}`

**Blocks:**
1. **Format String** — `"Hello {{payload.name}}!"` → `output_var: greeting`
2. **Set Return Value** — `{{vars.greeting}}`

**Output:** `"Hello Alice!"`

---

### Example 2: Age Check API

**Input:** `{"age": 16}`

**Blocks:**
1. **Condition** — `{{payload.age}} >= 18` → `output_var: is_adult`
2. **Stop If** — `{{vars.is_adult}}` is false → error: `"Must be 18+"`
3. **Set Return Value** — `{"status": "welcome"}`

**Output (age < 18):** `{"error": "Must be 18+"}`

---

### Example 3: External API Call

**Input:** `{"username": "octocat"}`

**Blocks:**
1. **HTTP Call** — `GET https://api.github.com/users/{{payload.username}}` → `output_var: user`
2. **Transform** — `{"name": "{{vars.user.body.name}}", "repos": "{{vars.user.body.public_repos}}"}`
3. **Set Return Value** — `{{vars.transform}}`

**Output:** `{"name": "The Octocat", "repos": 8}`

---

## Testing Your API

### From the Studio
1. Open the **Test** tab
2. Enter a JSON payload
3. Click **Run Test**
4. View the response and block execution trace

### From the Command Line

```bash
# Using API ID
curl -X POST http://localhost:5000/run/abc123 \
  -H "Content-Type: application/json" \
  -d '{"name": "Test"}'

# Using custom route
curl -X POST http://localhost:5000/api/my-api \
  -H "Content-Type: application/json" \
  -d '{"name": "Test"}'
```

### With Authentication

```bash
# API Key
curl -X POST http://localhost:5000/run/abc123 \
  -H "X-API-Key: your-key" \
  -d '{"data": "test"}'

# Bearer Token
curl -X POST http://localhost:5000/run/abc123 \
  -H "Authorization: Bearer your-token" \
  -d '{"data": "test"}'
```

---

## Input Validation

Define expected fields in the **Inputs** tab:

```json
[
  {"name": "email", "type": "string", "required": true, "max_length": 255},
  {"name": "age",   "type": "number", "required": true, "min_value": 0}
]
```

---

## Response Configuration

Configure output behaviour in the **Response** tab:

| Setting | Effect |
|---|---|
| Output variable | Which variable to return |
| Wrap in key | Nests the response: `{"data": {...}}` |
| Include metadata | Adds timestamp and duration |

---

## Error Handling

Each block has an `on_error` setting:

- **Stop** *(default)* — Halt execution on error
- **Continue** — Skip the failed block and keep going

---

## Workflow Examples

**Webhook Processor**
```
Input → Filter active items → Call analytics API → Call CRM → Return summary
```

**User Registration**
```
Input → Validate email → Check if exists → Create user → Send welcome email → Return user data
```

**Data Enrichment**
```
Input product ID → Fetch product data → Fetch reviews → Combine → Return enriched data
```

---

## Tips & Best Practices

- **Test incrementally** — Test each block as you add it
- **Use descriptive variable names** — `user_data`, not `x`
- **Handle errors gracefully** — Use `continue` for non-critical operations
- **Keep blocks simple** — One responsibility per block
- **Validate inputs** — Always define input validation rules

---

## Troubleshooting

| Problem | Solution |
|---|---|
| Block not found | Check block type spelling |
| Expression not working | Use `{{vars.name}}`, not `{{name}}` |
| JSON error | Check for trailing commas; use valid JSON |
| API not saving | Check `apis_store.json` permissions |
| Wrong response | Add a **Set Return Value** block explicitly |

---

## File Storage

All APIs are saved to `apis_store.json`. **Back up this file to preserve your work.**

---

## Quick Reference

### Essential Patterns

| Goal | Block to use |
|---|---|
| Store data | Set Variable |
| Make a decision | Condition |
| Stop early | Stop If |
| Call external service | HTTP Call |
| Format output | Transform |
| Set response | Set Return Value |

### Expression Examples

```
{{payload.user.name}}
{{vars.api_response.body.id}}
{{query.page}}
{{headers.X-Token}}
{{vars.count + 1}}
```

### Common HTTP Status Codes

| Code | Meaning |
|---|---|
| `200` | Success |
| `400` | Bad request |
| `401` | Auth failed |
| `422` | Validation error |
| `500` | Server error |

---

## Need Help?

- Check the **Logs** tab for execution history
- Use the **Test** tab to debug with traces
- Review error messages in the response
- Check block configuration for typos

---

> **Start building:** [http://localhost:5000](http://localhost:5000)
