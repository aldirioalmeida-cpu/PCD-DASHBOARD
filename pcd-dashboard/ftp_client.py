# -*- coding: utf-8 -*-
"""
ftp_client.py
-------------
Cliente para consultar dados de PCDs celulares (modelo DualBase) que
depositam arquivos num servidor FTP (ex: webfiles.inema.ba.gov.br).

Estrutura real do servidor (confirmada por inspeção manual):

    /FTPCONSULTA/PCD-PLU/
        PG-PR-03/                              <- uma pasta por estação,
        VJ-PR-19/                                 nomeada com o código INEMA
        ...
            PG-PR-03_TABELA_2608061900.txt     <- um arquivo por hora,
            PG-PR-03_TABELA_2608062000.txt        AAMMDDHHmm no nome
            ...

Cada arquivo tem várias linhas CSV (uma leitura a cada ~10 min), com a
data/hora real da leitura já na própria linha:

    "2026-08-06 18:10:00","PG-PR-03",14.04,32.16,...,"VIVO","...","..."

Estratégia: a data/hora usada como referência de "última transmissão" vem
SEMPRE do conteúdo da linha (campo 1), nunca do nome do arquivo nem da
data de modificação do FTP — o conteúdo é o dado real e mais preciso.
Esse horário já vem em UTC (confirmado) — sem conversão de fuso, no
mesmo padrão usado no resto da aplicação (GOES/DDS também é UTC).
"""

from __future__ import annotations

import csv
import ftplib
import io
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

BRT_OFFSET = timedelta(hours=-3)  # Horário de Brasília, sem DST desde 2019


class FtpClientError(Exception):
    """Erro genérico de conexão/comunicação com o servidor FTP."""


class FtpAbortedError(FtpClientError):
    """A operação foi interrompida pelo usuário."""


@dataclass
class FtpEntry:
    name: str
    size: Optional[int]
    mtime: Optional[datetime]  # UTC, conforme reportado pelo servidor (informativo apenas)
    is_dir: bool


@dataclass
class Reading:
    timestamp_utc: datetime
    raw_line: str
    source_file: str


# ---------------------------------------------------------------------------
# Sessão FTP (permite reaproveitar a mesma conexão pra várias estações)
# ---------------------------------------------------------------------------

class FtpSession:
    def __init__(self, host: str, username: str, password: str, port: int = 21, timeout: float = 20.0):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self.ftp: Optional[ftplib.FTP] = None
        self._lock = threading.Lock()
        self._aborted = False

    def __enter__(self) -> "FtpSession":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def connect(self) -> None:
        try:
            ftp = ftplib.FTP()
            ftp.connect(self.host, self.port, timeout=self.timeout)
            ftp.login(self.username, self.password)
            self.ftp = ftp
        except ftplib.error_perm as e:
            raise FtpClientError(f"Falha de autenticação FTP: {e}")
        except OSError as e:
            raise FtpClientError(f"Não foi possível conectar a {self.host}:{self.port} — {e}")
        except ftplib.all_errors as e:
            raise FtpClientError(f"Erro de comunicação FTP com {self.host}:{self.port} — {e}")

    def close(self) -> None:
        with self._lock:
            ftp, self.ftp = self.ftp, None
        if ftp:
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass

    def abort(self) -> None:
        """Interrompe a conexão de outra thread (botão 'Parar')."""
        self._aborted = True
        with self._lock:
            ftp, self.ftp = self.ftp, None
        if ftp and ftp.sock:
            try:
                ftp.sock.close()
            except Exception:
                pass

    def _check_aborted(self):
        if self._aborted:
            raise FtpAbortedError("Consulta interrompida pelo usuário.")

    # -- listagem -------------------------------------------------------

    def list_directory(self, path: str) -> list[FtpEntry]:
        self._check_aborted()
        ftp = self.ftp
        if ftp is None:
            raise FtpClientError("Sessão FTP não está conectada.")
        entries: list[FtpEntry] = []
        try:
            for name, facts in ftp.mlsd(path):
                if name in (".", ".."):
                    continue
                mtime = _parse_mlsd_time(facts.get("modify", "")) if "modify" in facts else None
                size = int(facts["size"]) if facts.get("size", "").isdigit() else None
                is_dir = facts.get("type") == "dir"
                entries.append(FtpEntry(name=name, size=size, mtime=mtime, is_dir=is_dir))
            return entries
        except (ftplib.error_perm, AttributeError) as e:
            self._check_aborted()
            if isinstance(e, ftplib.error_perm) and "550" in str(e):
                raise FtpClientError(f"Não foi possível acessar o diretório '{path}': {e}")
            # servidor não suporta MLSD — cai para o parsing de LIST abaixo
        except OSError:
            self._check_aborted()
            raise
        except ftplib.all_errors as e:
            self._check_aborted()
            raise FtpClientError(f"Erro ao listar '{path}': {e}")

        lines: list[str] = []
        try:
            ftp.retrlines(f"LIST {path}", lines.append)
        except OSError:
            self._check_aborted()
            raise
        except ftplib.all_errors as e:
            self._check_aborted()
            raise FtpClientError(f"Erro ao listar '{path}': {e}")
        now = datetime.now(timezone.utc)
        for line in lines:
            if not line.strip():
                continue
            entry = _parse_msdos_list_line(line) or _parse_unix_list_line(line, now)
            if entry and entry.name not in (".", ".."):
                entries.append(entry)
        return entries

    def fetch_file_text(self, path: str, filename: str, max_bytes: int = 500_000) -> str:
        self._check_aborted()
        ftp = self.ftp
        if ftp is None:
            raise FtpClientError("Sessão FTP não está conectada.")
        chunks: list[bytes] = []
        total = 0

        def _collect(data: bytes):
            nonlocal total
            if total < max_bytes:
                chunks.append(data)
                total += len(data)

        full_path = path.rstrip("/") + "/" + filename
        try:
            ftp.retrbinary(f"RETR {full_path}", _collect)
        except OSError:
            self._check_aborted()
            raise
        except ftplib.all_errors as e:
            self._check_aborted()
            raise FtpClientError(f"Não foi possível baixar '{filename}': {e}")

        raw = b"".join(chunks)
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("latin-1", errors="replace")


# ---------------------------------------------------------------------------
# Parsing de baixo nível (datas do LIST/MLSD — só informativo/fallback)
# ---------------------------------------------------------------------------

def _parse_mlsd_time(val: str) -> Optional[datetime]:
    try:
        return datetime.strptime(val[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


_LIST_DATE_RE = re.compile(
    r"(?P<month>\w{3})\s+(?P<day>\d{1,2})\s+(?:(?P<year>\d{4})|(?P<hour>\d{1,2}):(?P<minute>\d{2}))"
)
_MONTHS = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,"jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}


def _parse_unix_list_line(line: str, now: datetime) -> Optional[FtpEntry]:
    parts = line.split(None, 8)
    if len(parts) < 9:
        return None
    is_dir = parts[0].startswith("d")
    try:
        size = int(parts[4])
    except ValueError:
        size = None
    name = parts[8]

    m = _LIST_DATE_RE.search(line)
    mtime = None
    if m:
        month = _MONTHS.get(m.group("month").lower())
        if month:
            day = int(m.group("day"))
            if m.group("year"):
                mtime = datetime(int(m.group("year")), month, day, tzinfo=timezone.utc)
            else:
                hour, minute = int(m.group("hour")), int(m.group("minute"))
                candidate = datetime(now.year, month, day, hour, minute, tzinfo=timezone.utc)
                if candidate > now:
                    candidate = candidate.replace(year=now.year - 1)
                mtime = candidate
    return FtpEntry(name=name, size=size, mtime=mtime, is_dir=is_dir)


# Formato MS-DOS/IIS, comum em servidores FTP Windows:
#   08-06-26  05:04PM       <DIR>          PG-PR-03
#   08-06-26  09:03AM               885 PG-PR-03_TABELA_2608061200.txt
_MSDOS_LIST_RE = re.compile(
    r"^(?P<month>\d{2})-(?P<day>\d{2})-(?P<year>\d{2,4})\s+"
    r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<ampm>AM|PM)\s+"
    r"(?:(?P<dir><DIR>)|(?P<size>\d+))\s+"
    r"(?P<name>.+)$",
    re.IGNORECASE,
)


def _parse_msdos_list_line(line: str) -> Optional[FtpEntry]:
    m = _MSDOS_LIST_RE.match(line.strip())
    if not m:
        return None

    year = int(m.group("year"))
    if year < 100:
        year += 2000
    month, day = int(m.group("month")), int(m.group("day"))
    hour, minute = int(m.group("hour")), int(m.group("minute"))
    if m.group("ampm").upper() == "PM" and hour != 12:
        hour += 12
    elif m.group("ampm").upper() == "AM" and hour == 12:
        hour = 0

    try:
        mtime = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
    except ValueError:
        mtime = None

    is_dir = m.group("dir") is not None
    size = int(m.group("size")) if m.group("size") else None
    name = m.group("name").strip()

    return FtpEntry(name=name, size=size, mtime=mtime, is_dir=is_dir)


# ---------------------------------------------------------------------------
# Nome de pasta/arquivo <-> estação
# ---------------------------------------------------------------------------

def normalize_code(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def find_station_folder(entries: list[FtpEntry], cod_inema: str) -> Optional[FtpEntry]:
    """Acha a subpasta cujo nome bate exatamente (ignorando maiúsc./separadores) com o código INEMA."""
    target = normalize_code(cod_inema)
    for e in entries:
        if e.is_dir and normalize_code(e.name) == target:
            return e
    return None


_FILENAME_RE = re.compile(r"_TABELA_(\d{10})\.txt$", re.IGNORECASE)


def parse_filename_datetime(filename: str) -> Optional[datetime]:
    """
    Extrai a data/hora embutida no nome do arquivo, no padrão
    '{CODIGO}_TABELA_AAMMDDHHmm.txt'. Usado só pra ORDENAR/escolher qual
    arquivo é o mais recente sem precisar baixar todos — a data de verdade
    usada nos resultados vem do conteúdo do arquivo (parse_readings).
    """
    m = _FILENAME_RE.search(filename)
    if not m:
        return None
    digits = m.group(1)
    try:
        yy, mm, dd, hh, mi = int(digits[0:2]), int(digits[2:4]), int(digits[4:6]), int(digits[6:8]), int(digits[8:10])
        year = 2000 + yy
        return datetime(year, mm, dd, hh, mi)  # naive, hora local (só pra ordenação)
    except ValueError:
        return None


def sort_files_newest_first(entries: list[FtpEntry]) -> list[FtpEntry]:
    files = [e for e in entries if not e.is_dir]

    def sort_key(e: FtpEntry):
        dt = parse_filename_datetime(e.name)
        if dt is not None:
            return (1, dt)
        if e.mtime is not None:
            return (0, e.mtime.replace(tzinfo=None))
        return (0, datetime.min)

    return sorted(files, key=sort_key, reverse=True)


# ---------------------------------------------------------------------------
# Parsing do conteúdo (linhas CSV com timestamp real da leitura)
# ---------------------------------------------------------------------------

_ROW_TS_RE = re.compile(r"^\s*\"?(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\"?")


def parse_readings(text: str, source_file: str, local_to_utc: bool = False) -> list[Reading]:
    """Extrai cada linha do arquivo como uma leitura com timestamp (campo 1).

    O timestamp gravado nos arquivos do FTP já vem em UTC (confirmado),
    então por padrão não há conversão. Se algum dia precisar tratar um
    conjunto de arquivos com timestamp em horário local (BRT, UTC-3),
    chame com local_to_utc=True.
    """
    readings: list[Reading] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _ROW_TS_RE.match(line)
        if not m:
            continue
        try:
            dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if local_to_utc:
            dt = dt - BRT_OFFSET  # BRT -> UTC (soma 3h, já que BRT_OFFSET é -3h)
        dt = dt.replace(tzinfo=timezone.utc)
        readings.append(Reading(timestamp_utc=dt, raw_line=line, source_file=source_file))
    return readings


def last_nonempty_line(text: str) -> Optional[str]:
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line:
            return line
    return None
