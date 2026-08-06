"""
dds_client.py
--------------
Implementação do protocolo DDS (DCP Data Service), usado pelos servidores
LRGS da NOAA (dcs1-4.noaa.gov, porta 16003) para distribuir mensagens de
PCDs (DCPs) recebidas via satélite GOES.

Baseado em: "DCP Data Service (DDS) Protocol Specification - Protocol
Version 14" (Cove Software / NOAA NESDIS, 2016) e no "LRGS User's Guide"
(OpenDCS). Não depende do OpenDCS/Java — é uma implementação direta do
protocolo binário sobre TCP.

Uso típico:
    from dds_client import DdsSession

    with DdsSession(host, port, username, password) as session:
        session.send_search_criteria(dcp_addresses, since_hours=48)
        messages = session.retrieve_all_messages()

Cada item de `messages` é um dict com os campos do cabeçalho DOMSAT de
37 bytes (endereço da PCD, horário, qualidade, força de sinal, etc.)
"""

from __future__ import annotations

import hashlib
import socket
import struct
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

SYNC = b"FAF0"
DOMSAT_HEADER_LEN = 37
DEFAULT_PORT = 16003

# Códigos de erro relevantes (ver Tabela 2-1 da especificação)
DUNTIL = "35"          # Fim do intervalo solicitado (não é erro real)
DMSGTIMEOUT = "11"      # Sem novas mensagens (modo tempo real)
DSTRONGREQUIRED = "55"  # Servidor exige SHA-256


class DdsError(Exception):
    """Erro genérico de protocolo/comunicação com o servidor DDS."""


class DdsAuthError(DdsError):
    """Falha de autenticação (usuário/senha inválidos)."""


class DdsAbortedError(DdsError):
    """A operação foi interrompida pelo usuário (botão 'Parar consulta')."""


# ---------------------------------------------------------------------------
# Baixo nível: framing de mensagens
# ---------------------------------------------------------------------------

def _make_message(type_code: bytes, body: bytes) -> bytes:
    if len(body) > 99999:
        raise DdsError("Corpo da mensagem excede o limite do protocolo (99999 bytes)")
    length = f"{len(body):05d}".encode("ascii")
    return SYNC + type_code + length + body


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise DdsError("Conexão encerrada pelo servidor antes do esperado")
        buf.extend(chunk)
    return bytes(buf)


def _recv_message(sock: socket.socket) -> tuple[bytes, bytes]:
    header = _recv_exact(sock, 10)
    if header[:4] != SYNC:
        raise DdsError(f"Cabeçalho inválido recebido do servidor: {header!r}")
    type_code = header[4:5]
    length = int(header[5:10])
    body = _recv_exact(sock, length) if length else b""
    return type_code, body


def _parse_error_body(body: bytes) -> tuple[str, str, str]:
    """Corpo de erro no formato '?ServerCode,SystemCode,explicação'."""
    text = body[1:].decode("ascii", errors="replace")
    parts = text.split(",", 2)
    server_code = parts[0].strip() if len(parts) > 0 else ""
    system_code = parts[1].strip() if len(parts) > 1 else ""
    explanation = parts[2].strip() if len(parts) > 2 else ""
    return server_code, system_code, explanation


# ---------------------------------------------------------------------------
# Autenticação (seção 3.3 da especificação)
# ---------------------------------------------------------------------------

def _yyjjjhhmmss(dt: datetime) -> str:
    """Formata data/hora UTC como YYDDDHHMMSS (dia juliano)."""
    return dt.strftime("%y") + dt.strftime("%j") + dt.strftime("%H%M%S")


def _build_authenticator(username: str, password: str, when: datetime, algo: str) -> str:
    """
    Constrói o autenticador conforme a implementação oficial (AuthenticatorString.java
    + PasswordFileEntry.java do OpenDCS). Ponto crítico, fácil de errar: o hash
    preliminar da senha (equivalente ao que fica armazenado no servidor) é
    SEMPRE SHA-1, independente do algoritmo escolhido — só o hash externo/final
    é que muda entre SHA-1 e SHA-256.
    """
    u = username.encode("ascii")
    p = password.encode("ascii")

    # Hash preliminar: SEMPRE SHA-1 (buildShaPassword usa digestAlgo="SHA" fixo)
    prelim = hashlib.sha1(u + p + u + p).digest()

    # Hash final: usa o algoritmo solicitado (SHA-1 ou SHA-256)
    outer_hashfn = hashlib.sha1 if algo == "sha1" else hashlib.sha256
    epoch = int(when.timestamp())
    time_bytes = struct.pack(">I", epoch)
    payload = u + prelim + time_bytes + u + prelim + time_bytes
    return outer_hashfn(payload).hexdigest().upper()


# ---------------------------------------------------------------------------
# Cabeçalho DOMSAT (37 bytes) — seção 6 da especificação
# ---------------------------------------------------------------------------

FAILURE_CODE_DESC = {
    "G": "Boa mensagem",
    "?": "Mensagem com erro de paridade",
    "W": "Recebida no canal errado",
    "D": "Duplicada (múltiplos canais)",
    "A": "Endereço corrigido automaticamente",
    "B": "Endereço desconhecido/inválido",
    "T": "Recebida fora do horário esperado",
    "U": "Recebida totalmente fora do horário esperado",
    "M": "Mensagem ausente (não recebida na janela esperada)",
    "I": "Endereço inválido",
    "N": "Entrada incompleta na tabela de plataformas (PDT)",
    "Q": "Medições de qualidade ruins",
}

DATA_QUALITY_DESC = {
    "N": "Normal",
    "F": "Razoável",
    "P": "Ruim",
}


@dataclass
class DcpMessage:
    dcp_address: str
    time_str: str          # YYDDDHHMMSS bruto
    timestamp: datetime    # convertido para datetime UTC
    failure_code: str
    signal_strength: str
    freq_offset: str
    mod_index: str
    data_quality: str
    channel: str
    spacecraft: str
    carrier_status: str
    msg_len: int
    data: bytes = field(repr=False, default=b"")

    @property
    def is_good(self) -> bool:
        return self.failure_code == "G"

    @property
    def failure_desc(self) -> str:
        return FAILURE_CODE_DESC.get(self.failure_code, f"Código desconhecido ({self.failure_code})")

    @property
    def quality_desc(self) -> str:
        return DATA_QUALITY_DESC.get(self.data_quality, self.data_quality)


def _parse_time_yyjjjhhmmss(s: str) -> datetime:
    yy = int(s[0:2])
    jjj = int(s[2:5])
    hh = int(s[5:7])
    mm = int(s[7:9])
    ss = int(s[9:11])
    year = 2000 + yy if yy < 70 else 1900 + yy
    return datetime(year, 1, 1, tzinfo=timezone.utc) + timedelta(days=jjj - 1, hours=hh, minutes=mm, seconds=ss)


def _parse_domsat_header(raw: bytes, data: bytes) -> DcpMessage:
    time_str = raw[8:19].decode("ascii")
    return DcpMessage(
        dcp_address=raw[0:8].decode("ascii"),
        time_str=time_str,
        timestamp=_parse_time_yyjjjhhmmss(time_str),
        failure_code=raw[19:20].decode("ascii"),
        signal_strength=raw[20:22].decode("ascii"),
        freq_offset=raw[22:24].decode("ascii"),
        mod_index=raw[24:25].decode("ascii"),
        data_quality=raw[25:26].decode("ascii"),
        channel=raw[26:29].decode("ascii"),
        spacecraft=raw[29:30].decode("ascii"),
        carrier_status=raw[30:32].decode("ascii"),
        msg_len=int(raw[32:37].decode("ascii")),
        data=data,
    )


# ---------------------------------------------------------------------------
# Sessão DDS de alto nível
# ---------------------------------------------------------------------------

class DdsSession:
    def __init__(self, host: str, username: str, password: str,
                 port: int = DEFAULT_PORT, timeout: float = 20.0):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._aborted = False

    def __enter__(self) -> "DdsSession":
        self.connect_and_authenticate()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    # -- conexão -----------------------------------------------------------

    def connect_and_authenticate(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        self.sock.settimeout(self.timeout)

        last_error = None
        for algo in ("sha1", "sha256"):
            if self._aborted:
                raise DdsAbortedError("Consulta interrompida pelo usuário.")
            now = datetime.now(timezone.utc)
            time_str = _yyjjjhhmmss(now)
            auth = _build_authenticator(self.username, self.password, now, algo)
            body = f"{self.username} {time_str} {auth}".encode("ascii")
            try:
                self.sock.sendall(_make_message(b"m", body))
                type_code, resp_body = _recv_message(self.sock)
            except (OSError, DdsError):
                if self._aborted:
                    raise DdsAbortedError("Consulta interrompida pelo usuário.")
                raise

            if type_code == b"m" and not resp_body.startswith(b"?"):
                return  # autenticado com sucesso

            if resp_body.startswith(b"?"):
                server_code, _, explanation = _parse_error_body(resp_body)
                if server_code == DSTRONGREQUIRED and algo == "sha1":
                    continue  # servidor exige SHA-256; tenta de novo com o algoritmo forte
                last_error = f"[{server_code}] {explanation or resp_body} (algoritmo tentado: {algo.upper()})"
                break
            else:
                last_error = f"Resposta inesperada: {resp_body!r} (algoritmo tentado: {algo.upper()})"
                break

        self.close()
        raise DdsAuthError(f"Falha na autenticação em {self.host}:{self.port} — {last_error}")

    def close(self) -> None:
        with self._lock:
            sock, self.sock = self.sock, None
        if sock:
            try:
                sock.sendall(_make_message(b"b", b""))
                _recv_message(sock)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    def abort(self) -> None:
        """
        Interrompe a conexão imediatamente, de forma segura para ser chamada
        de OUTRA thread enquanto esta sessão está bloqueada esperando dados
        do servidor (ex: botão "Parar consulta" na interface). Não tenta
        enviar "goodbye" — apenas derruba o socket, o que faz qualquer
        recv() bloqueado nesta sessão levantar uma exceção imediatamente.
        """
        self._aborted = True
        with self._lock:
            sock, self.sock = self.sock, None
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass

    # -- critério de busca ---------------------------------------------------

    def send_search_criteria(self, dcp_addresses: list[str], since_hours: float) -> None:
        """Filtra por lista de endereços de PCD e uma janela de tempo (até agora)."""
        assert self.sock is not None
        lines = [
            f"DRS_SINCE: now - {since_hours} hours",
            "DRS_UNTIL: now",
        ]
        for addr in dcp_addresses:
            lines.append(f"DCP_ADDRESS: {addr.strip().upper()}")
        crit_text = "\n".join(lines) + "\n"

        body = b" " * 50 + crit_text.encode("ascii")
        self.sock.sendall(_make_message(b"g", body))
        type_code, resp_body = _recv_message(self.sock)
        if resp_body.startswith(b"?"):
            server_code, _, explanation = _parse_error_body(resp_body)
            raise DdsError(f"Erro ao enviar critério de busca [{server_code}]: {explanation or resp_body}")

    def send_search_criteria_absolute(self, dcp_addresses: list[str],
                                       since_dt: datetime, until_dt: datetime) -> None:
        """Filtra por lista de endereços de PCD e um intervalo absoluto [since_dt, until_dt) em UTC."""
        assert self.sock is not None

        def fmt(dt: datetime) -> str:
            return dt.strftime("%Y/%j %H:%M:%S")  # formato absoluto: YYYY/DDD HH:MM:SS

        lines = [f"DRS_SINCE: {fmt(since_dt)}", f"DRS_UNTIL: {fmt(until_dt)}"]
        for addr in dcp_addresses:
            lines.append(f"DCP_ADDRESS: {addr.strip().upper()}")
        crit_text = "\n".join(lines) + "\n"

        body = b" " * 50 + crit_text.encode("ascii")
        self.sock.sendall(_make_message(b"g", body))
        type_code, resp_body = _recv_message(self.sock)
        if resp_body.startswith(b"?"):
            server_code, _, explanation = _parse_error_body(resp_body)
            raise DdsError(f"Erro ao enviar critério de busca [{server_code}]: {explanation or resp_body}")

    # -- recuperação de mensagens --------------------------------------------

    def retrieve_all_messages(self, round_timeout: float = 20.0) -> list[DcpMessage]:
        """
        Recupera todas as mensagens que atendem ao critério enviado
        anteriormente, usando o modo de bloco (IdDcpBlock / 'n').
        Encerra quando o servidor sinaliza fim do intervalo (DUNTIL).
        """
        assert self.sock is not None
        self.sock.settimeout(round_timeout)
        messages: list[DcpMessage] = []

        while True:
            if self._aborted:
                raise DdsAbortedError("Consulta interrompida pelo usuário.")
            try:
                self.sock.sendall(_make_message(b"n", b""))
                type_code, body = _recv_message(self.sock)
            except socket.timeout:
                break
            except (OSError, DdsError):
                if self._aborted:
                    raise DdsAbortedError("Consulta interrompida pelo usuário.")
                raise

            if type_code != b"n":
                break

            if body.startswith(b"?"):
                server_code, _, explanation = _parse_error_body(body)
                if server_code in (DUNTIL, DMSGTIMEOUT):
                    break
                raise DdsError(f"Erro do servidor [{server_code}]: {explanation or body}")

            offset = 0
            n = len(body)
            got_any = False
            while offset + DOMSAT_HEADER_LEN <= n:
                header_raw = body[offset:offset + DOMSAT_HEADER_LEN]
                msg_len = int(header_raw[32:37])
                data_start = offset + DOMSAT_HEADER_LEN
                data_end = data_start + msg_len
                if data_end > n:
                    break  # mensagem partida entre blocos (não deveria ocorrer, mas por segurança)
                data = body[data_start:data_end]
                messages.append(_parse_domsat_header(header_raw, data))
                got_any = True
                offset = data_end

            if not got_any:
                break

        return messages

    # -- utilitário de alto nível ---------------------------------------------

    def latest_per_dcp(self, dcp_addresses: list[str], since_hours: float) -> dict[str, Optional[DcpMessage]]:
        """Retorna, para cada endereço solicitado, a mensagem mais recente
        recebida dentro da janela (ou None se nenhuma foi encontrada)."""
        self.send_search_criteria(dcp_addresses, since_hours)
        messages = self.retrieve_all_messages()

        latest: dict[str, Optional[DcpMessage]] = {addr.strip().upper(): None for addr in dcp_addresses}
        for msg in messages:
            addr = msg.dcp_address.upper()
            if addr not in latest:
                continue
            current = latest[addr]
            if current is None or msg.timestamp > current.timestamp:
                latest[addr] = msg
        return latest

    def coverage_per_dcp(self, dcp_addresses: list[str], since_dt: datetime,
                          until_dt: datetime, expected_interval_hours: float = 1.0) -> dict[str, dict]:
        """
        Calcula, para cada PCD, o percentual de transmissões recebidas em
        relação ao esperado no intervalo [since_dt, until_dt), assumindo
        que cada PCD deveria transmitir a cada `expected_interval_hours`
        horas (padrão: 1h).

        Retorna um dict por endereço com:
            expected      -> nº de transmissões esperadas no intervalo
            received      -> nº de "slots" (horas) com pelo menos 1 mensagem válida
            pct           -> percentual (0-100)
            total_messages-> total de mensagens (incl. reenviadas/duplicadas no slot)
        """
        if until_dt <= since_dt:
            raise ValueError("O horário final deve ser posterior ao horário inicial")

        self.send_search_criteria_absolute(dcp_addresses, since_dt, until_dt)
        messages = self.retrieve_all_messages()

        addrs_upper = [a.strip().upper() for a in dcp_addresses]
        total_seconds = (until_dt - since_dt).total_seconds()
        expected = max(1, round(total_seconds / (expected_interval_hours * 3600)))

        # Para cada PCD, agrupamos mensagens válidas (G ou ?) por "slot" de hora
        slots_by_addr: dict[str, set] = {a: set() for a in addrs_upper}
        total_by_addr: dict[str, int] = {a: 0 for a in addrs_upper}

        for msg in messages:
            addr = msg.dcp_address.upper()
            if addr not in slots_by_addr:
                continue
            if msg.failure_code not in ("G", "?"):
                continue  # ignora mensagens de status do DAPS, conta só transmissões reais
            elapsed_hours = (msg.timestamp - since_dt).total_seconds() / 3600.0
            slot = int(elapsed_hours // expected_interval_hours)
            slots_by_addr[addr].add(slot)
            total_by_addr[addr] += 1

        result = {}
        for addr in addrs_upper:
            received = len(slots_by_addr[addr])
            pct = round(100.0 * received / expected, 1) if expected else 0.0
            result[addr] = {
                "expected": expected,
                "received": received,
                "pct": min(pct, 100.0),
                "total_messages": total_by_addr[addr],
            }
        return result
