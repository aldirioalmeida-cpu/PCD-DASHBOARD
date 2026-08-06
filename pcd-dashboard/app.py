"""
app.py
------
Dashboard web para monitorar PCDs (Plataformas de Coleta de Dados) que
transmitem via satélite GOES, consultando diretamente um servidor
LRGS/DDS da NOAA (dcs1-4.noaa.gov, porta 16003 por padrão).

Como rodar:
    pip install -r requirements.txt
    python app.py

Depois abra http://localhost:5000 no navegador.

IMPORTANTE:
- Isto roda 100% localmente. As credenciais informadas no formulário são
  enviadas do navegador para este servidor Flask local, e deste servidor
  diretamente para o LRGS da NOAA — nunca são armazenadas em disco.
- Requer que a máquina onde este app roda tenha acesso de saída à porta
  TCP 16003 para o host do LRGS (verifique firewall/rede).
- Não é um produto oficial da NOAA/NESDIS.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from dds_client import DdsAbortedError, DdsAuthError, DdsError, DdsSession

app = Flask(__name__)

# SECRET_KEY assina o cookie de sessão. Em produção, defina uma variável de
# ambiente SECRET_KEY com um valor aleatório longo (ex: openssl rand -hex 32).
app.secret_key = os.environ.get("SECRET_KEY", "dev-only-change-me")

# APP_PASSWORD protege o acesso ao painel. Se não for definida, o app fica
# aberto sem login (ok para uso local; defina sempre em produção/deploy).
APP_PASSWORD = os.environ.get("APP_PASSWORD")

STATIONS_PATH = os.path.join(os.path.dirname(__file__), "stations.json")
CATEGORIES_PATH = os.path.join(os.path.dirname(__file__), "categories.json")

ADDRESS_RE = re.compile(r"^[0-9A-Fa-f]{8}$")

# Registro de operações em andamento, para suportar o botão "Parar consulta".
# NOTA: isso vive na memória de UM processo. Funciona perfeitamente rodando
# localmente ou com "gunicorn --workers 1". Com múltiplos workers, o pedido
# de abortar pode cair num processo diferente do que está rodando a consulta.
_active_ops_lock = threading.Lock()
_active_ops: dict[str, DdsSession] = {}


def _register_op(request_id: str, sess: DdsSession) -> None:
    if not request_id:
        return
    with _active_ops_lock:
        _active_ops[request_id] = sess


def _unregister_op(request_id: str) -> None:
    if not request_id:
        return
    with _active_ops_lock:
        _active_ops.pop(request_id, None)


def _load_stations() -> list[dict]:
    try:
        with open(STATIONS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return []


def _load_categories() -> dict:
    try:
        with open(CATEGORIES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _category_of(address: str, categories: dict) -> str | None:
    for name, addrs in categories.items():
        if address in addrs:
            return name
    return None


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if APP_PASSWORD and not session.get("authenticated"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if not APP_PASSWORD:
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        if request.form.get("password") == APP_PASSWORD:
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "Senha incorreta."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.pop("authenticated", None)
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


@app.route("/api/stations")
@login_required
def api_stations():
    stations = _load_stations()
    categories = _load_categories()
    for s in stations:
        s["categoria"] = _category_of(s["address"], categories)
    return jsonify(stations)


@app.route("/api/categories")
@login_required
def api_categories():
    return jsonify(_load_categories())


@app.route("/api/abort", methods=["POST"])
@login_required
def api_abort():
    payload = request.get_json(force=True, silent=True) or {}
    request_id = payload.get("request_id")
    with _active_ops_lock:
        sess = _active_ops.get(request_id)
    if sess is None:
        return jsonify({"aborted": False, "reason": "Operação não encontrada (já terminou?)"}), 404
    sess.abort()
    return jsonify({"aborted": True})


@app.route("/api/query", methods=["POST"])
@login_required
def api_query():
    payload = request.get_json(force=True, silent=True) or {}

    host = (payload.get("host") or "").strip()
    port = int(payload.get("port") or 16003)
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    lookback_hours = float(payload.get("lookback_hours") or 48)
    threshold_hours = float(payload.get("threshold_hours") or 6)
    raw_addresses = payload.get("dcp_addresses") or []
    request_id = payload.get("request_id") or str(uuid.uuid4())

    dcp_addresses = [a.strip().upper() for a in raw_addresses if a.strip()]

    if not host:
        return jsonify({"error": "Informe o host do LRGS (ex: dcs1.noaa.gov)"}), 400
    if not username:
        return jsonify({"error": "Informe o usuário DDS"}), 400
    if not dcp_addresses:
        return jsonify({"error": "Informe pelo menos um endereço de PCD"}), 400

    stations = {s["address"]: s for s in _load_stations()}

    sess = DdsSession(host, username, password, port=port)
    _register_op(request_id, sess)
    try:
        sess.connect_and_authenticate()
        latest = sess.latest_per_dcp(dcp_addresses, since_hours=lookback_hours)
    except DdsAbortedError:
        return jsonify({"error": "Consulta interrompida pelo usuário.", "aborted": True}), 499
    except DdsAuthError as e:
        return jsonify({"error": f"Falha de autenticação: {e}"}), 401
    except DdsError as e:
        return jsonify({"error": f"Erro de comunicação com o LRGS: {e}"}), 502
    except (TimeoutError, OSError) as e:
        return jsonify({"error": f"Não foi possível conectar a {host}:{port} — {e}"}), 502
    finally:
        sess.close()
        _unregister_op(request_id)

    now = datetime.now(timezone.utc)
    results = []
    for addr, msg in latest.items():
        station = stations.get(addr, {})
        base = {
            "dcp_address": addr,
            "municipio": station.get("municipio") or None,
            "nome_estacao": station.get("nome_estacao") or None,
            "cod_inema": station.get("cod_inema") or None,
        }
        if msg is None:
            results.append({
                **base,
                "last_transmission": None,
                "age_hours": None,
                "data_quality": None,
                "data_quality_desc": None,
                "signal_strength": None,
                "failure_code": None,
                "failure_desc": None,
                "channel": None,
                "spacecraft": None,
                "data_text": None,
                "status": "sem_transmitir",
            })
            continue

        age_hours = (now - msg.timestamp).total_seconds() / 3600.0
        status = "sem_transmitir" if age_hours > threshold_hours else "ok"
        results.append({
            **base,
            "last_transmission": msg.timestamp.isoformat(),
            "age_hours": round(age_hours, 2),
            "data_quality": msg.data_quality,
            "data_quality_desc": msg.quality_desc,
            "signal_strength": msg.signal_strength,
            "failure_code": msg.failure_code,
            "failure_desc": msg.failure_desc,
            "channel": msg.channel,
            "spacecraft": msg.spacecraft,
            "data_text": msg.data.decode("ascii", errors="replace").strip() if msg.data else None,
            "status": status,
        })

    # PCDs com maior atraso primeiro
    results.sort(key=lambda r: (r["age_hours"] is not None, r["age_hours"]), reverse=True)

    return jsonify({
        "queried_at": now.isoformat(),
        "lookback_hours": lookback_hours,
        "threshold_hours": threshold_hours,
        "results": results,
    })


@app.route("/api/coverage", methods=["POST"])
@login_required
def api_coverage():
    payload = request.get_json(force=True, silent=True) or {}

    host = (payload.get("host") or "").strip()
    port = int(payload.get("port") or 16003)
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    interval_hours = float(payload.get("interval_hours") or 1.0)
    recent_hours = float(payload.get("recent_hours") or 6.0)
    start_str = payload.get("start")
    end_str = payload.get("end")
    raw_addresses = payload.get("dcp_addresses") or []
    request_id = payload.get("request_id") or str(uuid.uuid4())

    dcp_addresses = [a.strip().upper() for a in raw_addresses if a.strip()]

    if not host:
        return jsonify({"error": "Informe o host do LRGS (ex: dcs1.noaa.gov)"}), 400
    if not username:
        return jsonify({"error": "Informe o usuário DDS"}), 400
    if not dcp_addresses:
        return jsonify({"error": "Informe pelo menos um endereço de PCD"}), 400
    if not start_str or not end_str:
        return jsonify({"error": "Informe o início e o fim do intervalo"}), 400

    try:
        start_dt = datetime.fromisoformat(start_str).replace(tzinfo=timezone.utc)
        end_dt = datetime.fromisoformat(end_str).replace(tzinfo=timezone.utc)
    except ValueError:
        return jsonify({"error": "Datas em formato inválido"}), 400

    if end_dt <= start_dt:
        return jsonify({"error": "O horário final deve ser posterior ao inicial"}), 400

    sess = DdsSession(host, username, password, port=port)
    _register_op(request_id, sess)
    try:
        sess.connect_and_authenticate()
        coverage = sess.coverage_per_dcp(
            dcp_addresses, start_dt, end_dt, expected_interval_hours=interval_hours
        )
        recent = sess.latest_per_dcp(dcp_addresses, since_hours=recent_hours)
    except DdsAbortedError:
        return jsonify({"error": "Consulta interrompida pelo usuário.", "aborted": True}), 499
    except DdsAuthError as e:
        return jsonify({"error": f"Falha de autenticação: {e}"}), 401
    except DdsError as e:
        return jsonify({"error": f"Erro de comunicação com o LRGS: {e}"}), 502
    except (TimeoutError, OSError) as e:
        return jsonify({"error": f"Não foi possível conectar a {host}:{port} — {e}"}), 502
    finally:
        sess.close()
        _unregister_op(request_id)

    stations = {s["address"]: s for s in _load_stations()}

    results = []
    for addr in dcp_addresses:
        c = coverage.get(addr, {"expected": 0, "received": 0, "pct": 0.0, "total_messages": 0})
        station = stations.get(addr, {})
        results.append({
            "dcp_address": addr,
            "label": station.get("label"),
            "municipio": station.get("municipio") or None,
            "nome_estacao": station.get("nome_estacao") or None,
            "cod_inema": station.get("cod_inema") or None,
            "expected": c["expected"],
            "received": c["received"],
            "pct": c["pct"],
            "total_messages": c["total_messages"],
        })

    results.sort(key=lambda r: r["pct"])  # piores primeiro

    transmitting_count = sum(1 for msg in recent.values() if msg is not None)
    total_stations = len(dcp_addresses)
    transmitting_pct = round(100.0 * transmitting_count / total_stations, 1) if total_stations else 0.0

    return jsonify({
        "start": start_dt.isoformat(),
        "end": end_dt.isoformat(),
        "interval_hours": interval_hours,
        "results": results,
        "transmitting_now": {
            "count": transmitting_count,
            "total": total_stations,
            "pct": transmitting_pct,
            "recent_hours": recent_hours,
        },
    })


@app.route("/api/fieldtest", methods=["POST"])
@login_required
def api_fieldtest():
    payload = request.get_json(force=True, silent=True) or {}

    host = (payload.get("host") or "").strip()
    port = int(payload.get("port") or 16003)
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    address = (payload.get("address") or "").strip().upper()
    lookback_hours = float(payload.get("lookback_hours") or 48)
    request_id = payload.get("request_id") or str(uuid.uuid4())

    if not host:
        return jsonify({"error": "Informe o host do LRGS (ex: cdadata.wcda.noaa.gov)"}), 400
    if not username:
        return jsonify({"error": "Informe o usuário DDS"}), 400
    if not ADDRESS_RE.match(address):
        return jsonify({"error": "Endereço inválido — precisa ser hexadecimal de 8 dígitos (ex: B0405296)."}), 400

    sess = DdsSession(host, username, password, port=port)
    _register_op(request_id, sess)
    try:
        sess.connect_and_authenticate()
        sess.send_search_criteria([address], since_hours=lookback_hours)
        messages = sess.retrieve_all_messages()
    except DdsAbortedError:
        return jsonify({"error": "Teste interrompido pelo usuário.", "aborted": True}), 499
    except DdsAuthError as e:
        return jsonify({"error": f"Falha de autenticação: {e}"}), 401
    except DdsError as e:
        return jsonify({"error": f"Erro de comunicação com o LRGS: {e}"}), 502
    except (TimeoutError, OSError) as e:
        return jsonify({"error": f"Não foi possível conectar a {host}:{port} — {e}"}), 502
    finally:
        sess.close()
        _unregister_op(request_id)

    # Só mensagens de fato da PCD (ignora notificações de status do DAPS)
    messages = [m for m in messages if m.dcp_address.upper() == address and m.failure_code in ("G", "?")]
    messages.sort(key=lambda m: m.timestamp, reverse=True)

    now = datetime.now(timezone.utc)
    stations = {s["address"]: s for s in _load_stations()}
    categories = _load_categories()
    station = stations.get(address, {})

    message_list = [{
        "timestamp": m.timestamp.isoformat(),
        "age_hours": round((now - m.timestamp).total_seconds() / 3600.0, 2),
        "data_quality": m.data_quality,
        "data_quality_desc": m.quality_desc,
        "failure_code": m.failure_code,
        "failure_desc": m.failure_desc,
        "signal_strength": m.signal_strength,
        "channel": m.channel,
        "spacecraft": m.spacecraft,
        "data_text": m.data.decode("ascii", errors="replace").strip() if m.data else None,
    } for m in messages]

    last = message_list[0] if message_list else None

    return jsonify({
        "address": address,
        "municipio": station.get("municipio") or None,
        "nome_estacao": station.get("nome_estacao") or None,
        "cod_inema": station.get("cod_inema") or None,
        "categoria": _category_of(address, categories),
        "known_station": address in stations,
        "lookback_hours": lookback_hours,
        "tested_at": now.isoformat(),
        "found": len(message_list) > 0,
        "message_count": len(message_list),
        "last_message": last,
        "messages": message_list,
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "1") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)
