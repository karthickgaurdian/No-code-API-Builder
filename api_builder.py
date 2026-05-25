"""
No-Code API Builder
====================
Run:  python3 api_builder.py
Then: open http://localhost:5000 in your browser

Pure Python + Flask (no other installs needed if Flask is present).
Auto-installs Flask if missing.
"""

import sys, subprocess

def ensure(pkg, import_name=None):
    try:
        __import__(import_name or pkg)
    except ImportError:
        cmd = [sys.executable, "-m", "pip", "install", pkg, "-q"]
        if sys.platform != "win32":
            cmd.append("--break-system-packages")
        subprocess.check_call(cmd)

ensure("flask")
ensure("requests")

# ── stdlib ──────────────────────────────────────────────────────────────
import json, uuid, time, hashlib, re, traceback, threading, logging
from datetime import datetime, timezone
from copy import deepcopy
from typing import Any
import urllib.request, urllib.error

# ── Flask ────────────────────────────────────────────────────────────────
from flask import Flask, request, jsonify, Response
import requests as req_lib

logging.basicConfig(level=logging.WARNING)
app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# ════════════════════════════════════════════════════════════════════════
#  DATA STORE  (in-memory, persists to apis.json on disk)
# ════════════════════════════════════════════════════════════════════════

import os
STORE_FILE = os.path.join(os.path.dirname(__file__), "apis_store.json")

def _load_store():
    if os.path.exists(STORE_FILE):
        try:
            with open(STORE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_store(data):
    with open(STORE_FILE, "w") as f:
        json.dump(data, f, indent=2)

API_STORE: dict[str, dict] = _load_store()
EXEC_LOGS: list[dict] = []          # recent execution logs (last 200)
_store_lock = threading.Lock()

def save_api(definition: dict):
    with _store_lock:
        API_STORE[definition["id"]] = definition
        _save_store(API_STORE)

def delete_api(api_id: str):
    with _store_lock:
        API_STORE.pop(api_id, None)
        _save_store(API_STORE)

def log_exec(entry: dict):
    EXEC_LOGS.append(entry)
    if len(EXEC_LOGS) > 200:
        EXEC_LOGS.pop(0)

# ════════════════════════════════════════════════════════════════════════
#  CONTEXT OBJECT  — shared state flowing through a block graph
# ════════════════════════════════════════════════════════════════════════

class Context:
    def __init__(self, payload: dict, path_params: dict, query_params: dict, headers: dict):
        self.payload      = payload
        self.path_params  = path_params
        self.query_params = query_params
        self.headers      = headers
        self.vars: dict   = {}          # block outputs live here
        self.result: Any  = None        # final return value
        self.meta: dict   = {}          # user-configured metadata
        self.errors: list = []          # accumulated non-fatal errors
        self.stopped      = False       # early-return flag

    def resolve(self, expr: str) -> Any:
        """Resolve {{vars.x}}, {{payload.y}}, {{query.z}}, {{path.w}} expressions."""
        if not isinstance(expr, str):
            return expr
        pattern = re.compile(r"\{\{(.+?)\}\}")
        def replace(m):
            path = m.group(1).strip().split(".")
            root = path[0]
            keys = path[1:]
            sources = {
                "vars":    self.vars,
                "payload": self.payload,
                "query":   self.query_params,
                "path":    self.path_params,
                "headers": self.headers,
            }
            obj = sources.get(root, {})
            for k in keys:
                if isinstance(obj, dict):
                    obj = obj.get(k, "")
                else:
                    obj = ""
            return str(obj)
        result = pattern.sub(replace, expr)
        # try to parse back to native type
        try:
            return json.loads(result)
        except Exception:
            return result

    def resolve_deep(self, obj: Any) -> Any:
        """Recursively resolve expressions in dicts/lists/strings."""
        if isinstance(obj, str):
            return self.resolve(obj)
        if isinstance(obj, dict):
            return {k: self.resolve_deep(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.resolve_deep(i) for i in obj]
        return obj

# ════════════════════════════════════════════════════════════════════════
#  BLOCK BASE + REGISTRY
# ════════════════════════════════════════════════════════════════════════

class BlockError(Exception):
    def __init__(self, code: str, message: str, details: Any = None):
        self.code    = code
        self.message = message
        self.details = details
        super().__init__(message)

BLOCK_REGISTRY: dict[str, type] = {}

def register_block(cls):
    BLOCK_REGISTRY[cls.type_id] = cls
    return cls

class BlockBase:
    type_id: str = ""
    label:   str = ""
    description: str = ""
    config_schema: dict = {}   # JSON-schema-lite for the GUI

    def validate_config(self, cfg: dict):
        """Called at definition time. Raise BlockError on bad config."""
        pass

    def validate_input(self, ctx: Context, cfg: dict):
        """Called before execute. Raise BlockError on bad runtime input."""
        pass

    def execute(self, ctx: Context, cfg: dict) -> Any:
        """Main logic. Return value is stored in ctx.vars[cfg['output_var']]."""
        raise NotImplementedError

    def run(self, ctx: Context, cfg: dict) -> dict:
        """Wrapper: validate → execute → store output. Returns trace entry."""
        start = time.time()
        output_var = cfg.get("output_var", "_last")
        try:
            self.validate_input(ctx, cfg)
            result = self.execute(ctx, cfg)
            ctx.vars[output_var] = result
            return {"block": cfg.get("name", self.type_id), "status": "ok",
                    "output_var": output_var, "ms": round((time.time()-start)*1000, 1)}
        except BlockError as e:
            ctx.errors.append({"block": cfg.get("name"), "code": e.code,
                                "message": e.message, "details": e.details})
            if cfg.get("on_error", "stop") == "stop":
                raise
            return {"block": cfg.get("name"), "status": "error",
                    "code": e.code, "message": e.message,
                    "ms": round((time.time()-start)*1000, 1)}
        except Exception as e:
            be = BlockError("BLOCK_EXCEPTION", str(e), traceback.format_exc())
            ctx.errors.append({"block": cfg.get("name"), "code": be.code,
                                "message": be.message})
            if cfg.get("on_error", "stop") == "stop":
                raise be
            return {"block": cfg.get("name"), "status": "error",
                    "code": be.code, "message": be.message,
                    "ms": round((time.time()-start)*1000, 1)}

# ════════════════════════════════════════════════════════════════════════
#  BLOCK IMPLEMENTATIONS
# ════════════════════════════════════════════════════════════════════════

@register_block
class SetVariableBlock(BlockBase):
    type_id = "set_variable"
    label   = "Set Variable"
    description = "Set a variable to a static value or expression."
    config_schema = {
        "value":      {"type": "text",   "label": "Value (can use {{expressions}})", "required": True},
        "output_var": {"type": "text",   "label": "Variable name to store result",   "required": True},
    }
    def execute(self, ctx, cfg):
        return ctx.resolve_deep(cfg.get("value", ""))

@register_block
class ConditionBlock(BlockBase):
    type_id = "condition"
    label   = "Condition (If/Else)"
    description = "Branch flow based on a comparison. Sets output_var to true/false."
    config_schema = {
        "left":       {"type": "text",   "label": "Left value (expression ok)",  "required": True},
        "operator":   {"type": "select", "label": "Operator",
                       "options": ["==","!=",">","<",">=","<=","contains","not_contains","exists"], "required": True},
        "right":      {"type": "text",   "label": "Right value (expression ok)", "required": False},
        "output_var": {"type": "text",   "label": "Variable name to store result","required": True},
    }
    def execute(self, ctx, cfg):
        left  = ctx.resolve(cfg.get("left", ""))
        right = ctx.resolve(cfg.get("right", ""))
        op    = cfg.get("operator", "==")
        try:
            l = json.loads(str(left)) if str(left).strip() else left
            r = json.loads(str(right)) if str(right).strip() else right
        except Exception:
            l, r = left, right
        ops = {
            "==": lambda a,b: a == b,
            "!=": lambda a,b: a != b,
            ">":  lambda a,b: float(a) > float(b),
            "<":  lambda a,b: float(a) < float(b),
            ">=": lambda a,b: float(a) >= float(b),
            "<=": lambda a,b: float(a) <= float(b),
            "contains":     lambda a,b: str(b) in str(a),
            "not_contains": lambda a,b: str(b) not in str(a),
            "exists":       lambda a,b: a not in (None, "", "null", "None"),
        }
        fn = ops.get(op, lambda a,b: False)
        try:
            return fn(l, r)
        except Exception as e:
            raise BlockError("CONDITION_ERROR", str(e))

@register_block
class StopIfBlock(BlockBase):
    type_id = "stop_if"
    label   = "Stop If"
    description = "Stop flow execution if a variable is true (or false)."
    config_schema = {
        "check_var":  {"type": "text",   "label": "Variable to check (e.g. vars.check)", "required": True},
        "stop_when":  {"type": "select", "label": "Stop when value is", "options": ["true","false"], "required": True},
        "error_code": {"type": "text",   "label": "Error code to return", "required": False},
        "error_msg":  {"type": "text",   "label": "Error message to return", "required": False},
        "output_var": {"type": "text",   "label": "Variable name (ignored)", "required": False},
    }
    def execute(self, ctx, cfg):
        val = ctx.resolve(cfg.get("check_var", ""))
        stop_when = cfg.get("stop_when", "true") == "true"
        should_stop = bool(val) == stop_when
        if should_stop:
            ctx.stopped = True
            code = cfg.get("error_code", "STOPPED") or "STOPPED"
            msg  = cfg.get("error_msg",  "Flow stopped by condition.") or "Flow stopped."
            raise BlockError(code, msg)
        return val

@register_block
class HttpCallBlock(BlockBase):
    type_id = "http_call"
    label   = "HTTP Call"
    description = "Call an external HTTP API."
    config_schema = {
        "url":        {"type": "text",   "label": "URL (expressions ok)",     "required": True},
        "method":     {"type": "select", "label": "Method",
                       "options": ["GET","POST","PUT","PATCH","DELETE"],       "required": True},
        "headers":    {"type": "textarea","label": "Headers (JSON object)",    "required": False},
        "body":       {"type": "textarea","label": "Body (JSON or expression)","required": False},
        "timeout":    {"type": "number", "label": "Timeout seconds",           "required": False},
        "output_var": {"type": "text",   "label": "Variable name to store result","required": True},
    }
    def execute(self, ctx, cfg):
        url     = ctx.resolve(cfg.get("url", ""))
        method  = cfg.get("method", "GET").upper()
        timeout = float(cfg.get("timeout") or 10)
        try:
            headers_raw = cfg.get("headers") or "{}"
            headers = json.loads(ctx.resolve(headers_raw)) if headers_raw.strip() else {}
        except Exception:
            headers = {}
        body = None
        if cfg.get("body"):
            raw_body = ctx.resolve_deep(cfg["body"])
            if isinstance(raw_body, str):
                try:
                    body = json.loads(raw_body)
                except Exception:
                    body = raw_body
            else:
                body = raw_body
        if not url:
            raise BlockError("MISSING_URL", "URL is required for HTTP call block.")
        try:
            resp = req_lib.request(method, url, headers=headers,
                                   json=body if isinstance(body, (dict,list)) else None,
                                   data=body if isinstance(body, str) else None,
                                   timeout=timeout)
            try:
                data = resp.json()
            except Exception:
                data = resp.text
            return {"status_code": resp.status_code, "body": data,
                    "headers": dict(resp.headers), "ok": resp.ok}
        except req_lib.exceptions.Timeout:
            raise BlockError("HTTP_TIMEOUT", f"Request to {url} timed out after {timeout}s.")
        except req_lib.exceptions.ConnectionError as e:
            raise BlockError("HTTP_CONNECTION_ERROR", str(e))
        except Exception as e:
            raise BlockError("HTTP_ERROR", str(e))

@register_block
class TransformBlock(BlockBase):
    type_id = "transform"
    label   = "Transform / Map Fields"
    description = "Build a new object by mapping/picking fields."
    config_schema = {
        "mapping":    {"type": "textarea","label": 'Output mapping (JSON). Values can use {{expressions}}.\nExample: {"name": "{{payload.first_name}}", "age": "{{vars.age}}"}', "required": True},
        "output_var": {"type": "text",    "label": "Variable name to store result", "required": True},
    }
    def execute(self, ctx, cfg):
        raw = cfg.get("mapping", "{}")
        try:
            mapping = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            raise BlockError("TRANSFORM_PARSE_ERROR", f"Mapping is not valid JSON: {e}")
        return ctx.resolve_deep(mapping)

@register_block
class FilterListBlock(BlockBase):
    type_id = "filter_list"
    label   = "Filter List"
    description = "Filter a list variable, keeping items where a field matches a value."
    config_schema = {
        "list_var":   {"type": "text",   "label": "Source list variable (e.g. {{vars.items}})", "required": True},
        "field":      {"type": "text",   "label": "Field name to check in each item",           "required": True},
        "operator":   {"type": "select", "label": "Operator",
                       "options": ["==","!=","contains","exists"],                               "required": True},
        "value":      {"type": "text",   "label": "Value to compare against",                   "required": False},
        "output_var": {"type": "text",   "label": "Variable name to store result",              "required": True},
    }
    def execute(self, ctx, cfg):
        lst = ctx.resolve(cfg.get("list_var",""))
        if not isinstance(lst, list):
            raise BlockError("NOT_A_LIST", f"Expected a list but got {type(lst).__name__}")
        field = cfg.get("field","")
        op    = cfg.get("operator","==")
        val   = ctx.resolve(cfg.get("value",""))
        def match(item):
            v = item.get(field) if isinstance(item, dict) else None
            if op == "==":       return v == val
            if op == "!=":       return v != val
            if op == "contains": return val in str(v)
            if op == "exists":   return v is not None
            return False
        return [i for i in lst if match(i)]

@register_block
class HashBlock(BlockBase):
    type_id = "hash"
    label   = "Hash Value"
    description = "Hash a value using md5 or sha256."
    config_schema = {
        "value":      {"type": "text",   "label": "Value to hash (expression ok)",       "required": True},
        "algorithm":  {"type": "select", "label": "Algorithm", "options": ["sha256","md5"], "required": True},
        "output_var": {"type": "text",   "label": "Variable name to store result",        "required": True},
    }
    def execute(self, ctx, cfg):
        val  = str(ctx.resolve(cfg.get("value","")))
        algo = cfg.get("algorithm","sha256")
        h = hashlib.sha256(val.encode()) if algo == "sha256" else hashlib.md5(val.encode())
        return h.hexdigest()

@register_block
class GenerateIdBlock(BlockBase):
    type_id = "generate_id"
    label   = "Generate UUID / Timestamp"
    description = "Generate a unique ID or current timestamp."
    config_schema = {
        "kind":       {"type": "select", "label": "What to generate",
                       "options": ["uuid4","timestamp_iso","timestamp_unix"], "required": True},
        "output_var": {"type": "text",   "label": "Variable name to store result", "required": True},
    }
    def execute(self, ctx, cfg):
        kind = cfg.get("kind","uuid4")
        if kind == "uuid4":          return str(uuid.uuid4())
        if kind == "timestamp_iso":  return datetime.now(timezone.utc).isoformat()
        if kind == "timestamp_unix": return int(time.time())

@register_block
class FormatStringBlock(BlockBase):
    type_id = "format_string"
    label   = "Format String"
    description = "Build a string by combining expressions."
    config_schema = {
        "template":   {"type": "textarea","label": "Template string (use {{expressions}})", "required": True},
        "output_var": {"type": "text",    "label": "Variable name to store result",         "required": True},
    }
    def execute(self, ctx, cfg):
        return ctx.resolve(cfg.get("template",""))

@register_block
class SetResultBlock(BlockBase):
    type_id = "set_result"
    label   = "Set Return Value"
    description = "Set what this API will return. Use in last block or before Stop If."
    config_schema = {
        "value":      {"type": "textarea","label": "Return value (JSON object or {{expression}})", "required": True},
        "output_var": {"type": "text",    "label": "Variable name (also sets ctx.result)",          "required": False},
    }
    def execute(self, ctx, cfg):
        val = ctx.resolve_deep(cfg.get("value",""))
        if isinstance(val, str):
            try:
                val = json.loads(val)
            except Exception:
                pass
        ctx.result = val
        return val

# ════════════════════════════════════════════════════════════════════════
#  FLOW EXECUTOR
# ════════════════════════════════════════════════════════════════════════

def execute_flow(api_def: dict, payload: dict, path_params: dict,
                 query_params: dict, headers: dict) -> dict:
    ctx   = Context(payload, path_params, query_params, headers)
    trace = []
    start = time.time()

    blocks = api_def.get("blocks", [])
    for block_cfg in blocks:
        if ctx.stopped:
            break
        btype = block_cfg.get("type")
        cls   = BLOCK_REGISTRY.get(btype)
        if not cls:
            entry = {"block": block_cfg.get("name", btype), "status": "error",
                     "code": "UNKNOWN_BLOCK", "message": f"Block type '{btype}' not found."}
            trace.append(entry)
            ctx.errors.append(entry)
            break
        try:
            t = cls().run(ctx, block_cfg)
            trace.append(t)
        except BlockError as e:
            trace.append({"block": block_cfg.get("name"), "status": "error",
                          "code": e.code, "message": e.message})
            total_ms = round((time.time()-start)*1000, 1)
            return _error_response(e.code, e.message, trace, total_ms,
                                   api_def.get("response_config", {}))
        except Exception as e:
            total_ms = round((time.time()-start)*1000, 1)
            return _error_response("INTERNAL_ERROR", str(e), trace, total_ms,
                                   api_def.get("response_config", {}))

    total_ms = round((time.time()-start)*1000, 1)
    return _build_response(ctx, api_def.get("response_config", {}), trace, total_ms)


def _build_response(ctx: Context, resp_cfg: dict, trace: list, ms: float) -> dict:
    result = ctx.result
    if result is None:
        output_var = resp_cfg.get("output_var")
        if output_var:
            result = ctx.vars.get(output_var)
        else:
            # default: last variable set
            result = list(ctx.vars.values())[-1] if ctx.vars else None

    meta = {}
    if resp_cfg.get("include_meta", False):
        meta["_meta"] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "duration_ms": ms,
        }
        extra = resp_cfg.get("meta_fields", {})
        if extra:
            resolved = ctx.resolve_deep(extra)
            meta["_meta"].update(resolved)

    if resp_cfg.get("wrap_key"):
        body = {resp_cfg["wrap_key"]: result}
    else:
        body = result if isinstance(result, dict) else {"result": result}

    if meta:
        body.update(meta)

    return {"ok": True, "status_code": 200, "body": body, "trace": trace, "ms": ms}


def _error_response(code: str, message: str, trace: list, ms: float, resp_cfg: dict) -> dict:
    body = {
        "error": {
            "code": code,
            "message": message,
        }
    }
    if resp_cfg.get("include_meta", False):
        body["_meta"] = {"timestamp": datetime.now(timezone.utc).isoformat(), "duration_ms": ms}
    return {"ok": False, "status_code": 400, "body": body, "trace": trace, "ms": ms}


# ════════════════════════════════════════════════════════════════════════
#  VALIDATION
# ════════════════════════════════════════════════════════════════════════

def validate_payload(payload: dict, input_config: list) -> tuple[bool, str]:
    for field in input_config:
        name     = field.get("name","")
        required = field.get("required", False)
        ftype    = field.get("type", "any")
        max_len  = field.get("max_length")
        min_val  = field.get("min_value")
        max_val  = field.get("max_value")

        val = payload.get(name)
        if required and val is None:
            return False, f"Field '{name}' is required."
        if val is None:
            continue
        if ftype == "string" and not isinstance(val, str):
            return False, f"Field '{name}' must be a string."
        if ftype == "number":
            try:
                val = float(val)
            except Exception:
                return False, f"Field '{name}' must be a number."
        if ftype == "boolean" and not isinstance(val, bool):
            return False, f"Field '{name}' must be a boolean."
        if ftype == "array" and not isinstance(val, list):
            return False, f"Field '{name}' must be an array."
        if ftype == "object" and not isinstance(val, dict):
            return False, f"Field '{name}' must be an object."
        if max_len and isinstance(val, str) and len(val) > int(max_len):
            return False, f"Field '{name}' exceeds max length of {max_len}."
        if min_val is not None:
            try:
                if float(val) < float(min_val):
                    return False, f"Field '{name}' must be >= {min_val}."
            except Exception:
                pass
        if max_val is not None:
            try:
                if float(val) > float(max_val):
                    return False, f"Field '{name}' must be <= {max_val}."
            except Exception:
                pass
    return True, ""


# ════════════════════════════════════════════════════════════════════════
#  STUDIO API  (the /studio/* endpoints)
# ════════════════════════════════════════════════════════════════════════

@app.route("/studio/apis", methods=["GET"])
def list_apis():
    return jsonify(list(API_STORE.values()))

@app.route("/studio/apis", methods=["POST"])
def create_api():
    data = request.json or {}
    api_id = str(uuid.uuid4())[:8]
    definition = {
        "id":            api_id,
        "name":          data.get("name", "Untitled API"),
        "description":   data.get("description", ""),
        "route":         data.get("route", f"/api/{api_id}"),
        "method":        data.get("method", "POST").upper(),
        "auth":          data.get("auth", "none"),
        "auth_key":      data.get("auth_key", ""),
        "input_config":  data.get("input_config", []),
        "blocks":        data.get("blocks", []),
        "response_config": data.get("response_config", {
            "output_var": "", "wrap_key": "", "include_meta": False, "meta_fields": {}
        }),
        "created_at":    datetime.now(timezone.utc).isoformat(),
        "updated_at":    datetime.now(timezone.utc).isoformat(),
    }
    save_api(definition)
    return jsonify(definition), 201

@app.route("/studio/apis/<api_id>", methods=["GET"])
def get_api(api_id):
    d = API_STORE.get(api_id)
    if not d:
        return jsonify({"error": "Not found"}), 404
    return jsonify(d)

@app.route("/studio/apis/<api_id>", methods=["PUT"])
def update_api(api_id):
    d = API_STORE.get(api_id)
    if not d:
        return jsonify({"error": "Not found"}), 404
    data = request.json or {}
    d.update(data)
    d["id"] = api_id
    d["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_api(d)
    return jsonify(d)

@app.route("/studio/apis/<api_id>", methods=["DELETE"])
def remove_api(api_id):
    delete_api(api_id)
    return jsonify({"ok": True})

@app.route("/studio/blocks", methods=["GET"])
def list_blocks():
    return jsonify([
        {"type_id": cls.type_id, "label": cls.label,
         "description": cls.description, "config_schema": cls.config_schema}
        for cls in BLOCK_REGISTRY.values()
    ])

@app.route("/studio/logs", methods=["GET"])
def get_logs():
    return jsonify(EXEC_LOGS[-50:])

@app.route("/studio/test/<api_id>", methods=["POST"])
def test_api(api_id):
    d = API_STORE.get(api_id)
    if not d:
        return jsonify({"error": "Not found"}), 404
    body = request.json or {}
    result = execute_flow(d, body, {}, dict(request.args), dict(request.headers))
    log_exec({"api_id": api_id, "api_name": d["name"], "at": datetime.now(timezone.utc).isoformat(),
              "ok": result["ok"], "ms": result["ms"], "source": "studio_test"})
    return jsonify(result)


# ════════════════════════════════════════════════════════════════════════
#  DYNAMIC API DISPATCHER  — all user-defined APIs land here
# ════════════════════════════════════════════════════════════════════════

def _handle_api(api_id: str, path_params: dict):
    d = API_STORE.get(api_id)
    if not d:
        return jsonify({"error": "API not found"}), 404

    # method check
    if request.method.upper() != d["method"].upper():
        return jsonify({"error": f"Method {request.method} not allowed"}), 405

    # auth
    auth_type = d.get("auth", "none")
    if auth_type == "api_key":
        key = request.headers.get("X-API-Key", "")
        if key != d.get("auth_key", ""):
            return jsonify({"error": "Unauthorized", "code": "INVALID_API_KEY"}), 401
    elif auth_type == "bearer":
        auth_hdr = request.headers.get("Authorization", "")
        token = auth_hdr.replace("Bearer ", "").strip()
        if token != d.get("auth_key", ""):
            return jsonify({"error": "Unauthorized", "code": "INVALID_TOKEN"}), 401

    # payload
    payload = {}
    if request.method.upper() in ("POST", "PUT", "PATCH"):
        payload = request.json or {}
    elif request.method.upper() == "GET":
        payload = dict(request.args)

    # validate inputs
    ok, msg = validate_payload(payload, d.get("input_config", []))
    if not ok:
        return jsonify({"error": {"code": "VALIDATION_ERROR", "message": msg}}), 422

    result = execute_flow(d, payload, path_params, dict(request.args), dict(request.headers))
    log_exec({"api_id": api_id, "api_name": d["name"], "at": datetime.now(timezone.utc).isoformat(),
              "ok": result["ok"], "ms": result["ms"], "source": "live"})
    return jsonify(result["body"]), result["status_code"]


@app.route("/run/<api_id>", methods=["GET","POST","PUT","PATCH","DELETE"])
def run_api_by_id(api_id):
    return _handle_api(api_id, {})

# ════════════════════════════════════════════════════════════════════════
#  GUI  — served at /
# ════════════════════════════════════════════════════════════════════════

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>API Builder Studio</title>
<style>
:root{
  --bg:#0f1117;--surface:#1a1d27;--surface2:#22263a;--border:#2d3150;
  --accent:#6c63ff;--accent2:#4ecca3;--danger:#ff5c5c;--warn:#f5a623;
  --text:#e8eaf6;--muted:#8892b0;--green:#4ecca3;--mono:'Fira Mono','Consolas',monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter','Segoe UI',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;display:flex}
/* ── sidebar ── */
#sidebar{width:220px;background:var(--surface);border-right:1px solid var(--border);display:flex;flex-direction:column;flex-shrink:0}
#sidebar header{padding:18px 16px 12px;border-bottom:1px solid var(--border)}
#sidebar header h1{font-size:15px;font-weight:700;color:var(--accent);letter-spacing:.5px}
#sidebar header p{font-size:11px;color:var(--muted);margin-top:3px}
#api-list{flex:1;overflow-y:auto;padding:8px}
.api-item{padding:9px 12px;border-radius:8px;cursor:pointer;margin-bottom:4px;border:1px solid transparent;transition:.15s}
.api-item:hover{background:var(--surface2);border-color:var(--border)}
.api-item.active{background:rgba(108,99,255,.15);border-color:var(--accent)}
.api-item .api-name{font-size:13px;font-weight:500}
.api-item .api-method{font-size:10px;padding:1px 6px;border-radius:4px;margin-top:3px;display:inline-block}
.method-GET{background:rgba(78,204,163,.15);color:var(--green)}
.method-POST{background:rgba(108,99,255,.15);color:var(--accent)}
.method-PUT{background:rgba(245,166,35,.15);color:var(--warn)}
.method-DELETE{background:rgba(255,92,92,.15);color:var(--danger)}
.method-PATCH{background:rgba(245,166,35,.12);color:#e6b84a}
#btn-new{margin:10px;padding:9px;background:var(--accent);color:#fff;border:none;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600}
#btn-new:hover{filter:brightness(1.1)}
/* ── main ── */
#main{flex:1;display:flex;flex-direction:column;overflow:hidden}
#tabs{display:flex;border-bottom:1px solid var(--border);background:var(--surface);padding:0 16px}
.tab{padding:12px 16px;cursor:pointer;font-size:13px;color:var(--muted);border-bottom:2px solid transparent;transition:.15s}
.tab.active{color:var(--text);border-bottom-color:var(--accent)}
#content{flex:1;overflow-y:auto;padding:24px}
/* ── forms ── */
.section{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:16px}
.section h2{font-size:14px;font-weight:600;color:var(--accent2);margin-bottom:14px;display:flex;align-items:center;gap:8px}
.field{margin-bottom:14px}
label{display:block;font-size:12px;color:var(--muted);margin-bottom:5px}
input,select,textarea{width:100%;background:var(--surface2);border:1px solid var(--border);border-radius:7px;padding:8px 10px;color:var(--text);font-size:13px;outline:none;transition:.15s}
input:focus,select:focus,textarea:focus{border-color:var(--accent);box-shadow:0 0 0 2px rgba(108,99,255,.2)}
textarea{resize:vertical;font-family:var(--mono);min-height:80px}
select option{background:var(--surface2)}
.row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.row3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px}
/* ── buttons ── */
.btn{padding:8px 16px;border-radius:7px;border:none;cursor:pointer;font-size:13px;font-weight:500;transition:.15s}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{filter:brightness(1.1)}
.btn-danger{background:rgba(255,92,92,.15);color:var(--danger);border:1px solid rgba(255,92,92,.3)}
.btn-ghost{background:transparent;color:var(--muted);border:1px solid var(--border)}
.btn-ghost:hover{color:var(--text);border-color:var(--muted)}
.btn-green{background:rgba(78,204,163,.15);color:var(--green);border:1px solid rgba(78,204,163,.3)}
/* ── blocks ── */
#blocks-list{display:flex;flex-direction:column;gap:8px}
.block-item{background:var(--surface2);border:1px solid var(--border);border-radius:9px;padding:12px 14px}
.block-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
.block-title{font-size:13px;font-weight:600}
.block-type-badge{font-size:10px;padding:2px 8px;background:rgba(108,99,255,.2);color:var(--accent);border-radius:20px}
.block-fields{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.block-fields .full{grid-column:1/-1}
/* ── add-block palette ── */
#block-palette{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px}
.palette-item{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px;cursor:pointer;transition:.15s}
.palette-item:hover{border-color:var(--accent);background:rgba(108,99,255,.08)}
.palette-item .p-label{font-size:12px;font-weight:600}
.palette-item .p-desc{font-size:10px;color:var(--muted);margin-top:3px}
/* ── test panel ── */
#test-in{font-family:var(--mono);min-height:120px}
#test-out{font-family:var(--mono);font-size:12px;min-height:160px;white-space:pre-wrap;background:var(--surface2);border:1px solid var(--border);border-radius:7px;padding:12px;color:var(--text)}
.trace-row{font-size:11px;padding:4px 8px;border-radius:4px;margin-bottom:3px;display:flex;gap:8px}
.trace-ok{background:rgba(78,204,163,.1);color:var(--green)}
.trace-err{background:rgba(255,92,92,.1);color:var(--danger)}
/* ── logs ── */
.log-row{font-size:12px;padding:6px 10px;border-bottom:1px solid var(--border);display:flex;gap:12px;align-items:center}
.log-ok{color:var(--green)}
.log-err{color:var(--danger)}
.log-ms{color:var(--muted);font-size:11px}
/* ── input fields config ── */
#input-fields-list{display:flex;flex-direction:column;gap:8px}
.input-field-row{background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;display:grid;grid-template-columns:1fr 1fr 1fr auto;gap:8px;align-items:end}
/* ── empty state ── */
#empty{display:flex;flex-direction:column;align-items:center;justify-content:center;height:100%;gap:12px;color:var(--muted)}
#empty h2{font-size:18px;color:var(--text)}
/* ── toast ── */
#toast{position:fixed;bottom:24px;right:24px;padding:10px 18px;background:var(--accent);color:#fff;border-radius:8px;font-size:13px;opacity:0;transition:.3s;pointer-events:none;z-index:999}
#toast.show{opacity:1}
/* ── scrollbar ── */
::-webkit-scrollbar{width:6px;height:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
/* ── response config ── */
.toggle-row{display:flex;align-items:center;gap:10px;margin-bottom:10px}
input[type=checkbox]{width:auto}
</style>
</head>
<body>

<div id="sidebar">
  <header>
    <h1>⚡ API Builder</h1>
    <p>No-code API Studio</p>
  </header>
  <div id="api-list"></div>
  <button id="btn-new" onclick="newApi()">+ New API</button>
</div>

<div id="main">
  <div id="tabs">
    <div class="tab active" onclick="showTab('route')">Route</div>
    <div class="tab" onclick="showTab('inputs')">Inputs</div>
    <div class="tab" onclick="showTab('flow')">Flow Builder</div>
    <div class="tab" onclick="showTab('response')">Response</div>
    <div class="tab" onclick="showTab('test')">Test</div>
    <div class="tab" onclick="showTab('logs')">Logs</div>
  </div>
  <div id="content">
    <div id="empty">
      <h2>Welcome to API Builder</h2>
      <p>Create a new API or select one from the sidebar.</p>
      <button class="btn btn-primary" onclick="newApi()">+ Create your first API</button>
    </div>
  </div>
</div>

<div id="toast"></div>

<script>
// ── STATE ──────────────────────────────────────────────────────────────
let currentApi = null;
let currentTab = 'route';
let blockDefs  = [];

// ── INIT ──────────────────────────────────────────────────────────────
async function init() {
  const [apis, blocks] = await Promise.all([
    fetch('/studio/apis').then(r=>r.json()),
    fetch('/studio/blocks').then(r=>r.json())
  ]);
  blockDefs = blocks;
  renderSidebar(apis);
}

// ── SIDEBAR ────────────────────────────────────────────────────────────
function renderSidebar(apis) {
  const el = document.getElementById('api-list');
  el.innerHTML = apis.length ? apis.map(a => `
    <div class="api-item ${currentApi && currentApi.id===a.id?'active':''}" onclick="selectApi('${a.id}')">
      <div class="api-name">${esc(a.name)}</div>
      <span class="api-method method-${a.method}">${a.method}</span>
    </div>`).join('') : '<p style="padding:12px;font-size:12px;color:var(--muted)">No APIs yet.</p>';
}

async function refreshSidebar() {
  const apis = await fetch('/studio/apis').then(r=>r.json());
  renderSidebar(apis);
}

// ── SELECT / NEW ───────────────────────────────────────────────────────
async function selectApi(id) {
  currentApi = await fetch(`/studio/apis/${id}`).then(r=>r.json());
  showTab(currentTab);
  await refreshSidebar();
}

async function newApi() {
  const name = prompt('API name:','My New API');
  if (!name) return;
  const route = prompt('Route path:',`/api/${name.toLowerCase().replace(/\s+/g,'-')}`);
  if (!route) return;
  const methods = ['GET','POST','PUT','PATCH','DELETE'];
  const method = prompt('Method (GET/POST/PUT/PATCH/DELETE):','POST').toUpperCase();
  if (!methods.includes(method)) { toast('Invalid method','warn'); return; }
  const api = await fetch('/studio/apis',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name,route,method})}).then(r=>r.json());
  currentApi = api;
  await refreshSidebar();
  showTab('route');
  toast('API created');
}

// ── TABS ──────────────────────────────────────────────────────────────
function showTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.tab').forEach((t,i)=>{
    t.classList.toggle('active', ['route','inputs','flow','response','test','logs'][i]===tab);
  });
  if (!currentApi && tab!=='logs') { document.getElementById('content').innerHTML = `<div id="empty"><h2>No API selected</h2><p>Create or select an API.</p></div>`; return; }
  const renders = {route:renderRoute,inputs:renderInputs,flow:renderFlow,response:renderResponse,test:renderTest,logs:renderLogs};
  (renders[tab]||renderLogs)();
}

// ── SAVE ──────────────────────────────────────────────────────────────
async function saveApi(patch) {
  const merged = {...currentApi, ...patch};
  currentApi = await fetch(`/studio/apis/${currentApi.id}`,{method:'PUT',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(merged)}).then(r=>r.json());
  await refreshSidebar();
  toast('Saved');
}

// ── TAB: ROUTE ────────────────────────────────────────────────────────
function renderRoute() {
  const a = currentApi;
  document.getElementById('content').innerHTML = `
  <div class="section">
    <h2>📡 Route Configuration</h2>
    <div class="row">
      <div class="field"><label>API Name</label><input id="r-name" value="${esc(a.name)}"></div>
      <div class="field"><label>HTTP Method</label>
        <select id="r-method">${['GET','POST','PUT','PATCH','DELETE'].map(m=>`<option${a.method===m?' selected':''}>${m}</option>`).join('')}</select>
      </div>
    </div>
    <div class="field"><label>Route Path</label><input id="r-route" value="${esc(a.route)}"></div>
    <div class="field"><label>Description</label><input id="r-desc" value="${esc(a.description||'')}"></div>
    <div class="row">
      <div class="field"><label>Authentication</label>
        <select id="r-auth" onchange="toggleAuthKey()">
          <option${a.auth==='none'?' selected':''}>none</option>
          <option value="api_key"${a.auth==='api_key'?' selected':''}>API Key (X-API-Key header)</option>
          <option value="bearer"${a.auth==='bearer'?' selected':''}>Bearer Token</option>
        </select>
      </div>
      <div class="field" id="auth-key-field" style="${a.auth==='none'?'display:none':''}">
        <label>Secret Key / Token</label><input id="r-authkey" value="${esc(a.auth_key||'')}" placeholder="your-secret-key">
      </div>
    </div>
    <div style="margin-top:8px;padding:10px;background:var(--surface2);border-radius:7px;font-size:12px;color:var(--muted)">
      Live endpoint: <code style="color:var(--accent2)">http://localhost:5000/run/${a.id}</code>
    </div>
  </div>
  <div style="display:flex;gap:10px">
    <button class="btn btn-primary" onclick="saveRoute()">Save Route</button>
    <button class="btn btn-danger" onclick="deleteApi()">Delete API</button>
  </div>`;
}

function toggleAuthKey() {
  const v = document.getElementById('r-auth').value;
  document.getElementById('auth-key-field').style.display = v==='none'?'none':'';
}

async function saveRoute() {
  await saveApi({
    name: document.getElementById('r-name').value,
    method: document.getElementById('r-method').value,
    route: document.getElementById('r-route').value,
    description: document.getElementById('r-desc').value,
    auth: document.getElementById('r-auth').value,
    auth_key: document.getElementById('r-authkey')?.value||'',
  });
}

async function deleteApi() {
  if (!confirm('Delete this API?')) return;
  await fetch(`/studio/apis/${currentApi.id}`,{method:'DELETE'});
  currentApi = null;
  await refreshSidebar();
  document.getElementById('content').innerHTML = `<div id="empty"><h2>Deleted</h2><p>Select or create an API.</p></div>`;
  toast('Deleted');
}

// ── TAB: INPUTS ───────────────────────────────────────────────────────
function renderInputs() {
  const fields = currentApi.input_config || [];
  document.getElementById('content').innerHTML = `
  <div class="section">
    <h2>📥 Input Configuration</h2>
    <p style="font-size:12px;color:var(--muted);margin-bottom:14px">Define expected fields in the request payload. All validations run automatically before your flow executes.</p>
    <div id="input-fields-list">${fields.map((f,i)=>inputFieldRow(f,i)).join('')}</div>
    <button class="btn btn-ghost" style="margin-top:10px" onclick="addInputField()">+ Add Field</button>
  </div>
  <button class="btn btn-primary" onclick="saveInputs()">Save Input Config</button>`;
}

function inputFieldRow(f, i) {
  return `<div class="input-field-row" id="ifield-${i}">
    <div class="field"><label>Field name</label><input value="${esc(f.name||'')}" onchange="updateInputField(${i},'name',this.value)"></div>
    <div class="field"><label>Type</label>
      <select onchange="updateInputField(${i},'type',this.value)">
        ${['any','string','number','boolean','array','object'].map(t=>`<option${f.type===t?' selected':''}>${t}</option>`).join('')}
      </select>
    </div>
    <div class="field"><label>Max length / Max value</label><input value="${esc(f.max_length||f.max_value||'')}" placeholder="optional" onchange="updateInputField(${i},'max_length',this.value)"></div>
    <div style="display:flex;flex-direction:column;gap:4px;align-items:center">
      <label style="font-size:11px">Required</label>
      <input type="checkbox" ${f.required?'checked':''} onchange="updateInputField(${i},'required',this.checked)" style="width:auto;margin-top:4px">
      <button class="btn btn-danger" style="padding:4px 8px;font-size:11px;margin-top:4px" onclick="removeInputField(${i})">✕</button>
    </div>
  </div>`;
}

function addInputField() {
  if (!currentApi.input_config) currentApi.input_config = [];
  currentApi.input_config.push({name:'',type:'string',required:false});
  renderInputs();
}
function updateInputField(i, key, val) {
  currentApi.input_config[i][key] = val;
}
function removeInputField(i) {
  currentApi.input_config.splice(i,1);
  renderInputs();
}
async function saveInputs() {
  await saveApi({input_config: currentApi.input_config});
}

// ── TAB: FLOW ─────────────────────────────────────────────────────────
function renderFlow() {
  const blocks = currentApi.blocks || [];
  document.getElementById('content').innerHTML = `
  <div class="section">
    <h2>🔧 Add Block</h2>
    <div id="block-palette">${blockDefs.map(b=>`
      <div class="palette-item" onclick="addBlock('${b.type_id}')">
        <div class="p-label">${esc(b.label)}</div>
        <div class="p-desc">${esc(b.description)}</div>
      </div>`).join('')}
    </div>
  </div>
  <div class="section">
    <h2>⚙️ Flow Steps (${blocks.length})</h2>
    <p style="font-size:12px;color:var(--muted);margin-bottom:12px">Blocks execute top to bottom. Use <code style="color:var(--accent2)">{{vars.name}}</code> to reference outputs between blocks.</p>
    <div id="blocks-list">${blocks.map((b,i)=>renderBlock(b,i)).join('')}</div>
  </div>
  <button class="btn btn-primary" onclick="saveFlow()">Save Flow</button>`;
}

function renderBlock(b, i) {
  const def = blockDefs.find(d=>d.type_id===b.type)||{config_schema:{}};
  const schema = def.config_schema || {};
  const fields = Object.entries(schema).map(([key,s])=>{
    const val = b[key]||'';
    const lbl = `<label>${esc(s.label||key)}</label>`;
    let inp = '';
    if (s.type==='select') {
      inp = `<select onchange="updateBlock(${i},'${key}',this.value)">${(s.options||[]).map(o=>`<option${val===o?' selected':''}>${o}</option>`).join('')}</select>`;
    } else if (s.type==='textarea') {
      inp = `<textarea onchange="updateBlock(${i},'${key}',this.value)">${esc(val)}</textarea>`;
    } else if (s.type==='number') {
      inp = `<input type="number" value="${esc(val)}" onchange="updateBlock(${i},'${key}',this.value)">`;
    } else {
      inp = `<input value="${esc(val)}" onchange="updateBlock(${i},'${key}',this.value)">`;
    }
    const isWide = s.type==='textarea' || (s.label||'').length > 40;
    return `<div class="field ${isWide?'full':''}">${lbl}${inp}</div>`;
  }).join('');

  return `<div class="block-item" id="block-${i}">
    <div class="block-header">
      <div style="display:flex;align-items:center;gap:8px">
        <span style="color:var(--muted);font-size:12px">#${i+1}</span>
        <span class="block-title">${esc(b.name||def.label||b.type)}</span>
        <span class="block-type-badge">${b.type}</span>
      </div>
      <div style="display:flex;gap:6px">
        ${i>0?`<button class="btn btn-ghost" style="padding:3px 8px;font-size:11px" onclick="moveBlock(${i},-1)">↑</button>`:''}
        ${i<((currentApi.blocks||[]).length-1)?`<button class="btn btn-ghost" style="padding:3px 8px;font-size:11px" onclick="moveBlock(${i},1)">↓</button>`:''}
        <button class="btn btn-danger" style="padding:3px 8px;font-size:11px" onclick="removeBlock(${i})">✕</button>
      </div>
    </div>
    <div class="field"><label>Block label</label>
      <input value="${esc(b.name||'')}" placeholder="${def.label}" onchange="updateBlock(${i},'name',this.value)">
    </div>
    <div class="block-fields">${fields}</div>
    <div class="field" style="margin-top:8px"><label>On error</label>
      <select onchange="updateBlock(${i},'on_error',this.value)">
        <option value="stop"${b.on_error!=='continue'?' selected':''}>Stop (return error response)</option>
        <option value="continue"${b.on_error==='continue'?' selected':''}>Continue (skip to next block)</option>
      </select>
    </div>
  </div>`;
}

function addBlock(typeId) {
  if (!currentApi.blocks) currentApi.blocks = [];
  const def = blockDefs.find(d=>d.type_id===typeId);
  const block = {type:typeId, name:'', on_error:'stop'};
  Object.keys(def.config_schema||{}).forEach(k=>block[k]='');
  currentApi.blocks.push(block);
  renderFlow();
}
function updateBlock(i, key, val) {
  currentApi.blocks[i][key] = val;
}
function removeBlock(i) {
  currentApi.blocks.splice(i,1);
  renderFlow();
}
function moveBlock(i, dir) {
  const b = currentApi.blocks;
  const j = i+dir;
  if (j<0||j>=b.length) return;
  [b[i],b[j]]=[b[j],b[i]];
  renderFlow();
}
async function saveFlow() {
  await saveApi({blocks: currentApi.blocks});
}

// ── TAB: RESPONSE ─────────────────────────────────────────────────────
function renderResponse() {
  const rc = currentApi.response_config || {};
  document.getElementById('content').innerHTML = `
  <div class="section">
    <h2>📤 Response Configuration</h2>
    <div class="field">
      <label>Output variable to return (leave blank for last variable set)</label>
      <input id="rc-outvar" value="${esc(rc.output_var||'')}" placeholder="e.g. result">
    </div>
    <div class="field">
      <label>Wrap response in key (leave blank for flat response)</label>
      <input id="rc-wrapkey" value="${esc(rc.wrap_key||'')}" placeholder="e.g. data">
    </div>
    <div class="toggle-row">
      <input type="checkbox" id="rc-meta" ${rc.include_meta?'checked':''}>
      <label for="rc-meta" style="margin:0;color:var(--text);font-size:13px">Include metadata (_meta) in response</label>
    </div>
    <div class="field">
      <label>Extra metadata fields (JSON object with expressions)</label>
      <textarea id="rc-metafields" placeholder='{"api_version": "1.0", "user": "{{headers.X-User-Id}}"}'>${esc(JSON.stringify(rc.meta_fields||{},null,2))}</textarea>
    </div>
    <div style="margin-top:12px;padding:12px;background:var(--surface2);border-radius:7px;font-size:12px">
      <div style="color:var(--muted);margin-bottom:6px">Response shape preview:</div>
      <code style="color:var(--accent2);white-space:pre">${buildResponsePreview(rc)}</code>
    </div>
  </div>
  <button class="btn btn-primary" onclick="saveResponseConfig()">Save Response Config</button>`;
}

function buildResponsePreview(rc) {
  const wrap = rc.wrap_key||'result';
  let obj = `{\n  "${wrap}": <your return value>`;
  if (rc.include_meta) obj += `,\n  "_meta": {\n    "timestamp": "...",\n    "duration_ms": 12\n  }`;
  obj += '\n}';
  return obj;
}

async function saveResponseConfig() {
  let meta_fields = {};
  try { meta_fields = JSON.parse(document.getElementById('rc-metafields').value||'{}'); } catch(e){}
  await saveApi({response_config:{
    output_var: document.getElementById('rc-outvar').value,
    wrap_key: document.getElementById('rc-wrapkey').value,
    include_meta: document.getElementById('rc-meta').checked,
    meta_fields,
  }});
}

// ── TAB: TEST ─────────────────────────────────────────────────────────
function renderTest() {
  const a = currentApi;
  const sample = {};
  (a.input_config||[]).forEach(f=>sample[f.name]='');
  document.getElementById('content').innerHTML = `
  <div class="section">
    <h2>🧪 Test API</h2>
    <p style="font-size:12px;color:var(--muted);margin-bottom:12px">
      Send a test request directly from the studio. Traces every block.
    </p>
    <div class="field"><label>Request payload (JSON)</label>
      <textarea id="test-in">${esc(JSON.stringify(sample,null,2))}</textarea>
    </div>
    <button class="btn btn-green" onclick="runTest()">▶ Run Test</button>
  </div>
  <div class="section" id="test-result-section" style="display:none">
    <h2>📊 Result</h2>
    <div id="test-out"></div>
    <div style="margin-top:12px"><h2 style="font-size:13px;color:var(--muted);margin-bottom:6px">Block trace</h2>
    <div id="test-trace"></div></div>
  </div>`;
}

async function runTest() {
  let payload = {};
  try { payload = JSON.parse(document.getElementById('test-in').value||'{}'); }
  catch(e) { toast('Invalid JSON in payload','err'); return; }
  const res = await fetch(`/studio/test/${currentApi.id}`,{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(r=>r.json());
  document.getElementById('test-result-section').style.display='';
  document.getElementById('test-out').textContent = JSON.stringify(res.body||res,null,2);
  document.getElementById('test-trace').innerHTML = (res.trace||[]).map(t=>`
    <div class="trace-row ${t.status==='error'?'trace-err':'trace-ok'}">
      <span>${t.block||'?'}</span>
      <span>${t.status}</span>
      ${t.output_var?`<span>→ ${t.output_var}</span>`:''}
      ${t.message?`<span>${esc(t.message)}</span>`:''}
      <span style="margin-left:auto">${t.ms}ms</span>
    </div>`).join('');
}

// ── TAB: LOGS ─────────────────────────────────────────────────────────
async function renderLogs() {
  const logs = await fetch('/studio/logs').then(r=>r.json());
  document.getElementById('content').innerHTML = `
  <div class="section">
    <h2>📋 Execution Logs (last 50)</h2>
    <div>${logs.length ? [...logs].reverse().map(l=>`
      <div class="log-row">
        <span class="${l.ok?'log-ok':'log-err'}">${l.ok?'OK':'ERR'}</span>
        <span>${esc(l.api_name||l.api_id)}</span>
        <span style="color:var(--muted)">${l.source||''}</span>
        <span class="log-ms">${l.ms}ms</span>
        <span style="color:var(--muted);font-size:11px;margin-left:auto">${l.at}</span>
      </div>`).join('') : '<p style="color:var(--muted);font-size:13px;padding:12px">No executions yet. Test an API or call a live endpoint.</p>'}
    </div>
  </div>`;
}

// ── UTILS ──────────────────────────────────────────────────────────────
function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function toast(msg, type='ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.style.background = type==='err'?'var(--danger)':type==='warn'?'var(--warn)':'var(--accent)';
  el.classList.add('show');
  setTimeout(()=>el.classList.remove('show'),2200);
}

init();
</script>
</body>
</html>"""

@app.route("/")
def studio():
    return HTML

# ════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"""
╔══════════════════════════════════════════════════╗
║         No-Code API Builder  — Studio            ║
╠══════════════════════════════════════════════════╣
║  Studio UI  →  http://localhost:{port}              ║
║  APIs store →  apis_store.json (auto-saved)      ║
╚══════════════════════════════════════════════════╝
""")
    app.run(host="0.0.0.0", port=port, debug=False)
