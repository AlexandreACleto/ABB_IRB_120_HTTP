"""Painel web para controlar vários IRB 120 (cada um rodando PyServer.mod) pela LAN.

Sobe um servidor HTTP que serve a página painel/index.html e uma API JSON. Qualquer
dispositivo da rede (PC, tablet, celular) abre o endereço mostrado ao iniciar e controla
os braços. Por segurança a API exige um token, impresso junto do link (use --no-token
para desligar).

Uso:
    python painel_robos.py                    # esq=192.168.0.11, dir=192.168.0.12, centro=192.168.0.14
    python painel_robos.py --robot a=192.168.0.11 --robot b=192.168.0.12
    python painel_robos.py --robot a=127.0.0.1:1025 --robot b=127.0.0.1:1026   # simulador

IMPORTANTE: o PyServer.mod só lê o próximo comando depois de terminar o movimento atual,
então este painel NÃO consegue interromper um movimento em andamento. A parada de
emergência é sempre o botão físico do robô / FlexPendant.
"""
import argparse
import hmac
import json
import math
import secrets
import socket
import threading
import time
import traceback
import webbrowser
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from multi_robo import parse_robot_args
from robo_cliente import (BOX_MAX, BOX_MIN, DEFAULT_PORT, JOINT_LIMITS, MAX_TCP_SPEED,
                          MIN_TCP_SPEED, IRB120Client, RobotError, check_joints,
                          check_linear_target, check_speed)

INDEX_HTML = Path(__file__).with_name("painel") / "index.html"
DEFAULT_SEQ_FILE = Path(__file__).with_name("sequencias.json")
DEFAULT_ROBOTS = ["esq=192.168.0.11", "dir=192.168.0.12", "centro=192.168.0.14"]
MAX_BODY = 64 * 1024

# Jog contínuo (controle de Xbox). Ajustes finos para fazer no robô real:
JOG_DT = 0.12        # duração nominal de cada passo encadeado (s)
JOG_SMOOTH = 0.35    # suavização da velocidade a cada passo (0 a 1): menor = rampa mais lenta
JOG_TIMEOUT = 0.20   # sem mensagem do painel por este tempo => o robô para no último destino (s)
JOG_MAX_SPEED = 100.0  # teto do controle deslizante no jog contínuo: quanto mais rápido, mais ele anda até parar
JOG_REACH = 400.0    # alcance médio (mm) para estimar a velocidade do TCP num passo de junta
JOG_ZONE_MAX = 20.0  # raio de curva máximo (mm) usado entre os passos


class Conflict(Exception):
    """Pedido recusado por causa do estado atual (robô offline ou ocupado)."""


class Arm:
    """Um robô: cliente TCP + estado mostrado no painel."""

    def __init__(self, name, host, port):
        self.name = name
        self.host = host
        self.port = port
        self.client = IRB120Client(host, port)
        # O socket do RAPID é sequencial: um comando por vez, senão as respostas se misturam
        self.lock = threading.Lock()
        self.connected = False
        self.busy = None  # texto do movimento em andamento, ou None
        self.pose = None
        self.joints = None
        self.home = None  # juntas lidas ao conectar: para onde o "voltar ao início" leva
        self.target = None  # destino em juntas do movimento em andamento, se for conhecido
        self.error = None

    def snapshot(self):
        return {"name": self.name, "host": self.host, "port": self.port,
                "connected": self.connected, "busy": self.busy,
                "pose": self.pose, "joints": self.joints, "home": self.home, "target": self.target,
                "error": self.error}


class JogSession:
    """Jog contínuo de um robô: o painel manda a deflexão do analógico, o servidor integra."""

    def __init__(self, mode, size, speed):
        self.mode = mode                  # "joints" (6 deflexões) ou "cart" (3: x, y, z)
        self.cmd = [0.0] * size           # deflexão pedida, de -1 a 1
        self.speed = speed                # controle deslizante do painel (mm/s)
        self.seen = time.monotonic()      # última mensagem do painel
        self.stop = False


class Panel:
    def __init__(self, robots, poll_period=0.5, seq_path=DEFAULT_SEQ_FILE):
        self.arms = {name: Arm(name, host, port) for name, (host, port) in robots.items()}
        self.poll_period = poll_period
        self.state_lock = threading.Lock()  # protege o par "checar livre -> marcar ocupado"
        self.events = deque(maxlen=80)
        self.event_seq = 0
        # Sequências gravadas: {nome: {"steps": [{"speed": v, "joints": {robô: [6 ângulos]}}]}}
        self.seq_path = Path(seq_path)
        self.seq_lock = threading.Lock()
        self.sequences = self._load_sequences()
        self.recording = None   # rascunho {"name", "steps"} enquanto grava
        self.playback = None    # {"name", "step", "total"} enquanto executa
        self.cancel = threading.Event()
        self.jogs = {}          # nome do robô -> JogSession em andamento

    # ---------- registro de eventos ----------

    def log(self, level, text, arm=None):
        with self.state_lock:
            self.event_seq += 1
            event = {"id": self.event_seq, "time": datetime.now().strftime("%H:%M:%S"),
                     "level": level, "robot": arm.name if arm else None, "text": text}
            self.events.append(event)
        print(f"[{event['time']}] {event['robot'] or '-':>6} {level.upper():5} {text}")

    # ---------- acesso ao robô (quem chama já segura arm.lock) ----------

    def _refresh(self, arm):
        arm.pose = arm.client.get_pose()
        arm.joints = arm.client.get_joints()

    def _drop(self, arm, err):
        """Perda de conexão: o socket pode estar fora de sincronia, então fecha de vez."""
        arm.client.close()
        arm.connected = False
        arm.error = f"Conexão perdida: {err}"
        self.log("error", arm.error, arm)

    def _guarded(self, arm, func):
        """Roda func(arm) tratando erro do robô (mantém conexão) e de rede (derruba)."""
        try:
            func(arm)
            return True
        except RobotError as err:
            arm.error = str(err)
            self.log("error", arm.error, arm)
        except (OSError, ValueError) as err:
            self._drop(arm, err)
        return False

    def poll_loop(self):
        """Atualiza pose/juntas dos robôs parados. Pula quem está em movimento."""
        while True:
            for arm in self.arms.values():
                if arm.connected and arm.lock.acquire(blocking=False):
                    try:
                        if arm.connected:
                            self._guarded(arm, self._refresh)
                    finally:
                        arm.lock.release()
            time.sleep(self.poll_period)

    # ---------- conexão ----------

    def _names(self, names):
        unknown = [n for n in names if n not in self.arms]
        if unknown or not names:
            raise RobotError(f"Robô desconhecido: {', '.join(unknown) or '(nenhum informado)'}")
        return [self.arms[n] for n in names]

    def connect(self, names):
        def work(arm):
            with arm.lock:
                if arm.connected:
                    return
                try:
                    arm.client.connect()
                    arm.connected = True
                    arm.error = None
                    self._refresh(arm)
                    arm.home = arm.joints
                    self.log("ok", f"Conectado a {arm.host}:{arm.port}", arm)
                except (OSError, ValueError, RobotError) as err:
                    arm.client.close()
                    arm.connected = False
                    arm.error = f"Falha ao conectar em {arm.host}:{arm.port}: {err}"
                    self.log("error", arm.error, arm)

        threads = [threading.Thread(target=work, args=(arm,)) for arm in self._names(names)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def disconnect(self, names):
        arms = self._names(names)
        if self.playback:
            raise Conflict("Há uma sequência em execução")
        for arm in arms:
            if arm.busy:
                raise Conflict(f"{arm.name} está em movimento; não dá para interromper por aqui")
        for arm in arms:
            with arm.lock:
                arm.client.close()
                arm.connected = False
                self.log("info", "Desconectado", arm)

    # ---------- movimento ----------

    def start_move(self, names, req, wait=False, from_sequence=False):
        """Valida e dispara o movimento em todos os robôs ao mesmo tempo.

        Tudo é planejado (e validado) antes de qualquer robô sair do lugar: se um dos
        pedidos for recusado, nenhum se move. Com wait=True só volta quando todos
        terminarem e devolve {nome: erro} dos que falharam durante o movimento.
        Enquanto uma sequência roda, só os passos dela podem mover os robôs.
        """
        arms = self._names(names)
        request = parse_move(req, [a.name for a in arms])
        if self.playback and not from_sequence:
            raise Conflict("Há uma sequência em execução")

        with self.state_lock:
            for arm in arms:
                if not arm.connected:
                    raise Conflict(f"{arm.name} não está conectado")
                if arm.busy:
                    raise Conflict(f"{arm.name} ainda está em movimento")
            for arm in arms:
                arm.busy = "planejando"

        plans = {}
        try:
            for arm in arms:
                with arm.lock:
                    if not arm.connected:
                        raise Conflict(f"{arm.name} não está conectado")
                    plans[arm.name] = self._plan(arm, request)
        except (RobotError, Conflict):
            self._release(arms)
            raise
        except (OSError, ValueError) as err:
            self._release(arms)
            for arm in arms:
                if arm.lock.acquire(timeout=1):
                    try:
                        self._drop(arm, err)
                    finally:
                        arm.lock.release()
            raise Conflict(f"Falha de comunicação: {err}")

        barrier = threading.Barrier(len(arms))
        outcomes = {}  # nome -> texto do erro, ou None se terminou bem
        threads = []
        for arm in arms:
            go, label, target = plans[arm.name]
            arm.target = target
            arm.busy = label
            self.log("info", f"Movendo: {label}", arm)
            thread = threading.Thread(target=self._run_move,
                                      args=(arm, go, label, barrier, outcomes), daemon=True)
            thread.start()
            threads.append(thread)
        if wait:
            for thread in threads:
                thread.join()
        return {name: err for name, err in outcomes.items() if err}

    def _release(self, arms):
        for arm in arms:
            arm.busy = None

    def _plan(self, arm, request):
        """Calcula o destino a partir do estado atual e valida.

        Devolve (go, rótulo, destino em juntas). O destino só é conhecido nos movimentos por
        juntas (None nos lineares: não há cinemática inversa aqui); o gêmeo digital o usa para
        mostrar onde o braço vai parar enquanto o PyServer não responde.
        """
        mode, speed = request["mode"], request["speed"]
        client = arm.client

        if mode == "home":
            home = arm.home
            if home is None:
                raise RobotError(f"{arm.name}: posição inicial desconhecida (reconecte)")
            check_joints(home)
            return ((lambda: client.move_joints(home, speed=speed)),
                    "voltar à posição inicial", list(home))

        if mode == "relative":
            dx, dy, dz = request["delta"]
            x, y, z, rx, ry, rz = client.get_pose()
            target = (x + dx, y + dy, z + dz, rx, ry, rz)
            check_linear_target(*target[:3])
            label = f"deslocamento ({dx:+g}, {dy:+g}, {dz:+g}) mm a {speed:g} mm/s"
            return (lambda: client.move_linear(*target, speed=speed)), label, None

        if mode == "joint_jog":
            joint, delta = request["joint"], request["delta"]
            joints = list(client.get_joints())
            joints[joint - 1] += delta
            check_joints(joints)
            label = f"J{joint} {delta:+g}° a {speed:g}"
            return (lambda: client.move_joints(joints, speed=speed)), label, list(joints)

        values = request["values"][arm.name]
        if mode == "linear":
            check_linear_target(*values[:3])
            label = "linear para " + " ".join(f"{v:g}" for v in values)
            return (lambda: client.move_linear(*values, speed=speed)), label, None

        check_joints(values)  # mode == "joints"
        label = "juntas para " + " ".join(f"{v:g}" for v in values)
        return (lambda: client.move_joints(values, speed=speed)), label, list(values)

    def _run_move(self, arm, go, label, barrier, outcomes):
        try:
            # Barreira: os comandos saem juntos para todos os robôs
            barrier.wait(timeout=10)
        except threading.BrokenBarrierError:
            outcomes[arm.name] = "movimento cancelado"
            arm.target = None
            arm.busy = None
            return
        with arm.lock:
            try:
                go()
                arm.error = None
                outcomes[arm.name] = None
                self.log("ok", f"Concluído: {label}", arm)
            except RobotError as err:
                arm.error = str(err)
                outcomes[arm.name] = arm.error
                self.log("error", arm.error, arm)
            except (OSError, ValueError) as err:
                self._drop(arm, err)
                outcomes[arm.name] = arm.error
            finally:
                if arm.connected:
                    self._guarded(arm, self._refresh)
                arm.target = None
                arm.busy = None

    # ---------- jog contínuo (interpolado) ----------
    # O PyServer.mod executa "vá até este ponto" e, com parada exata (fine), o robô para em cada passo:
    # movimento "anda-para". Aqui os passos são encadeados com raio de curva, então o robô emenda um
    # no outro sem parar. Para isso:
    #  - cada destino sai do destino anterior (não da posição lida, que fica atrasada);
    #  - a velocidade acompanha o analógico com suavização (rampa de aceleração e de parada);
    #  - o painel manda a deflexão ~20x por segundo; sem mensagem por JOG_TIMEOUT, o robô desacelera e
    #    para (parada exata no último destino).

    def jog(self, names, req):
        arms = self._names(names)
        if req.get("action") == "stop":
            for arm in arms:
                sess = self.jogs.get(arm.name)
                if sess:
                    sess.stop = True
            return
        mode, vel, speed = parse_jog(req)
        speed = min(speed, JOG_MAX_SPEED)

        fresh = []
        with self.state_lock:
            if self.playback:
                raise Conflict("Há uma sequência em execução")
            for arm in arms:
                if arm.name in self.jogs:
                    continue
                if not arm.connected:
                    raise Conflict(f"{arm.name} não está conectado")
                if arm.busy:
                    raise Conflict(f"{arm.name} ainda está em movimento")
                fresh.append(arm)
            for arm in fresh:
                self.jogs[arm.name] = JogSession(mode, len(vel), speed)
                arm.busy = "controle contínuo"

        for arm in arms:
            sess = self.jogs.get(arm.name)
            if sess is None:
                continue  # acabou de encerrar: a próxima mensagem abre outra sessão
            if sess.mode != mode:
                sess.stop = True  # trocou de modo no meio: termina esta; o painel abre outra
                continue
            sess.cmd, sess.speed, sess.seen, sess.stop = vel, speed, time.monotonic(), False
        for arm in fresh:
            threading.Thread(target=self._jog_run, args=(arm, self.jogs[arm.name]), daemon=True).start()

    def _jog_run(self, arm, sess):
        client = arm.client
        joints = pose = None
        moved, steps, warned, last_speed = False, 0, set(), MIN_TCP_SPEED
        vel = [0.0] * len(sess.cmd)
        last_send = 0.0
        with arm.lock:
            try:
                self.log("info", "Controle contínuo iniciado", arm)
                joints, pose = list(client.get_joints()), list(client.get_pose())
                while True:
                    # Limitador de cadência: o robô respondendo mais rápido que o passo nominal (ou um
                    # robô que não obedece ao limite de velocidade) não pode fazer o destino disparar.
                    wait = last_send + JOG_DT * 0.9 - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                    live = not sess.stop and time.monotonic() - sess.seen < JOG_TIMEOUT
                    cmd = sess.cmd if live else [0.0] * len(vel)
                    scale = sess.speed * (0.5 if sess.mode == "joints" else 1.0)  # graus/s ou mm/s
                    eps = max(0.05, 0.02 * scale)
                    if not any(cmd):
                        # Solto: não estende o destino com uma rampa de descida (isso faria o robô andar
                        # mais depois de soltar). Para no último destino já enviado, na desaceleração do robô.
                        vel[:] = [0.0] * len(vel)
                    for i, d in enumerate(cmd):  # subida e variações com o analógico: rampa suave
                        vel[i] += (d * scale - vel[i]) * JOG_SMOOTH
                        if d == 0 and abs(vel[i]) < eps:
                            vel[i] = 0.0

                    if not any(vel):
                        if moved:  # parou de pedir: parada exata no último destino
                            self._jog_settle(arm, sess.mode, joints, pose, last_speed)
                            moved = False
                        if not live:
                            break
                        time.sleep(0.02)
                        continue

                    step = self._jog_step(arm, sess.mode, vel, joints, pose, warned)
                    if step is None:  # no limite: nada a mover neste passo
                        time.sleep(0.02)
                        continue
                    new, speed, zone = step
                    last_send = time.monotonic()
                    if sess.mode == "joints":
                        client.move_joints(new, speed=speed, zone=zone)
                        joints, arm.joints = new, list(new)
                    else:
                        client.move_linear(*new, speed=speed, zone=zone)
                        pose, arm.pose = new, list(new)
                    moved, last_speed, steps = True, speed, steps + 1
                    if steps % 3 == 0:  # de tempos em tempos lê o outro lado, para os cartões e o gêmeo
                        if sess.mode == "joints":
                            arm.pose = client.get_pose()
                        else:
                            arm.joints = client.get_joints()
            except RobotError as err:
                arm.error = str(err)
                if "ERR ARGS" in arm.error:
                    arm.error = ("O programa do robô é a versão antiga, sem jog contínuo. Recarregue o PyServer.mod "
                                 "atual da pasta rapid no robô e rode o PyMain de novo.")
                self.log("error", arm.error, arm)
            except (OSError, ValueError) as err:
                self._drop(arm, err)
            finally:
                if arm.connected:
                    self._guarded(arm, self._refresh)
                with self.state_lock:
                    self.jogs.pop(arm.name, None)
                    arm.target = None
                    arm.busy = None
                self.log("info", "Controle contínuo encerrado", arm)

    def _jog_step(self, arm, mode, vel, joints, pose, warned):
        """Próximo destino (novo, velocidade do TCP, raio de curva) ou None se não há o que mover."""
        if mode == "joints":
            new = []
            for i, (cur, v) in enumerate(zip(joints, vel)):
                want = cur + v * JOG_DT
                low, high = JOINT_LIMITS[i]
                limited = min(max(want, low), high)
                if limited != want:
                    vel[i] = 0.0
                    if ("J", i) not in warned:
                        warned.add(("J", i))
                        self.log("info", f"J{i + 1} chegou ao limite ({limited:g}°)", arm)
                new.append(limited)
            biggest = max(abs(a - b) for a, b in zip(new, joints))
            if biggest < 0.01:
                return None
            length = math.radians(biggest) * JOG_REACH          # mm de TCP, estimativa grosseira
            speed = max(length / JOG_DT, 2 * biggest / JOG_DT)  # o 2º termo mantém J4 a J6 fora do limite do TCP
        else:
            new = [pose[0] + vel[0] * JOG_DT, pose[1] + vel[1] * JOG_DT, pose[2] + vel[2] * JOG_DT] + list(pose[3:])
            try:
                check_linear_target(*new[:3])
            except RobotError as err:
                vel[:] = [0.0] * len(vel)
                if "area" not in warned:
                    warned.add("area")
                    self.log("info", f"Limite da área de trabalho: {err}", arm)
                return None
            length = math.dist(new[:3], pose[:3])
            if length < 0.02:
                return None
            speed = length / JOG_DT
        return new, min(max(speed, MIN_TCP_SPEED), MAX_TCP_SPEED), min(max(0.5 * length, 1.0), JOG_ZONE_MAX)

    def _jog_settle(self, arm, mode, joints, pose, speed):
        """Parada exata no último destino (sem raio de curva): fecha o encadeamento."""
        if mode == "joints":
            arm.client.move_joints(joints, speed=speed)
        else:
            arm.client.move_linear(*pose, speed=speed)

    # ---------- sequências: movimentos gravados ----------
    # Cada ponto guarda as 6 juntas dos braços que estavam selecionados na hora de gravar.
    # A execução leva todos os braços do ponto juntos (MOVEJ), um ponto por vez.

    def _load_sequences(self):
        try:
            data = json.loads(self.seq_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as err:
            print(f"Não consegui ler {self.seq_path}: {err}. Começando sem sequências.")
            return {}

    def _save_sequences(self, data=None):
        """Grava o conjunto de sequências em disco (por padrão o atual) numa troca atômica."""
        data = self.sequences if data is None else data
        tmp = self.seq_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
        ultimo = None
        for _ in range(6):  # antivírus, OneDrive ou um editor podem segurar o arquivo por instantes
            try:
                tmp.replace(self.seq_path)  # troca atômica: nunca fica um arquivo pela metade
                return
            except PermissionError as err:
                ultimo = err
                time.sleep(0.25)
        tmp.unlink(missing_ok=True)
        raise OSError(f"não consegui gravar {self.seq_path.name}: o arquivo está em uso por outro programa. "
                      f"Feche-o (editor, OneDrive, antivírus) e tente de novo. ({ultimo})")

    def seq_action(self, body):
        """Operações de gravação/execução. Devolve um dict extra para a resposta."""
        action = body.get("action")
        if action == "add":
            # Fora do seq_lock: ler os robôs é lento e travaria a atualização da página (/api/state)
            return self._rec_add(body.get("robots") or [], body.get("speed", 20))
        with self.seq_lock:
            if action == "start":
                return self._rec_start(str(body.get("name", "")).strip())
            if action == "undo":
                steps = self._need_recording()["steps"]
                if steps:
                    steps.pop()
                return {"steps": len(steps)}
            if action == "discard":
                self.recording = None
                return {}
            if action == "save":
                return self._rec_save(str(body.get("name", "")).strip())
            if action == "delete":
                if self.playback:
                    raise Conflict("Há uma sequência em execução")
                nome = str(body.get("name"))
                if nome not in self.sequences:
                    raise RobotError("Sequência não encontrada")
                restantes = {k: v for k, v in self.sequences.items() if k != nome}
                self._save_sequences(restantes)  # primeiro no disco: se falhar, nada muda
                self.sequences = restantes
                return {}
            if action == "play":
                return self._play_start(str(body.get("name")), body.get("speed", 20))
            if action == "stop":
                self.cancel.set()  # vale entre pontos: o ponto em andamento termina
                return {}
        raise RobotError(f"Ação de sequência desconhecida: {action!r}")

    def _need_recording(self):
        if not self.recording:
            raise RobotError("Nenhuma gravação em andamento: inicie uma no painel")
        return self.recording

    def _rec_start(self, name):
        if self.playback:
            raise Conflict("Há uma sequência em execução")
        self.recording = {"name": name or f"Sequência {len(self.sequences) + 1}", "steps": []}
        return {}

    def _rec_add(self, names, speed):
        with self.seq_lock:
            self._need_recording()
        try:
            speed = check_speed(float(speed))
        except (TypeError, ValueError):
            raise RobotError("Velocidade inválida") from None
        arms = self._names(names)
        # Braço desconectado não derruba a gravação: fica de fora do ponto e o painel avisa quais ficaram
        offline = [a.name for a in arms if not a.connected]
        arms = [a for a in arms if a.connected]
        if not arms:
            raise Conflict("Nenhum dos braços escolhidos está conectado: conecte ou escolha outro braço")
        for arm in arms:
            if arm.busy:
                raise Conflict(f"{arm.name} ainda está em movimento")
        joints = {}
        for arm in arms:  # leitura fresca: o que vale é onde o robô está agora
            if not arm.lock.acquire(timeout=3):
                raise Conflict(f"{arm.name} está ocupado; tente de novo")
            try:
                if not (arm.connected and self._guarded(arm, self._refresh)):
                    raise Conflict(f"Não consegui ler as juntas de {arm.name}")
                joints[arm.name] = list(arm.joints)
            finally:
                arm.lock.release()
        with self.seq_lock:
            rec = self._need_recording()  # pode ter sido descartada enquanto líamos os robôs
            rec["steps"].append({"speed": speed, "joints": joints})
            total, nome = len(rec["steps"]), rec["name"]
        self.log("info", f"Ponto {total} gravado em '{nome}'"
                         + (f" (sem {', '.join(offline)}: desconectado)" if offline else ""))
        return {"steps": total, "ignorados": offline}

    def _rec_save(self, name):
        rec = self._need_recording()
        if not rec["steps"]:
            raise RobotError("A gravação não tem nenhum ponto")
        name = name or rec["name"]
        if len(name) > 60:
            raise RobotError("Nome grande demais (máx. 60 caracteres)")
        novas = {**self.sequences, name: {"steps": rec["steps"]}}
        self._save_sequences(novas)  # primeiro no disco: se falhar, a gravação continua aberta e dá para tentar de novo
        self.sequences = novas
        self.recording = None
        self.log("ok", f"Sequência '{name}' salva ({len(novas[name]['steps'])} pontos)")
        return {}

    def _play_start(self, name, speed):
        if self.playback:
            raise Conflict("Já há uma sequência em execução")
        if self.recording:
            raise Conflict("Termine ou descarte a gravação antes de executar")
        seq = self.sequences.get(name)
        if not seq:
            raise RobotError("Sequência não encontrada")
        try:
            speed = check_speed(float(speed))
        except (TypeError, ValueError):
            raise RobotError("Velocidade inválida") from None
        # Valida tudo antes de mover qualquer coisa
        involved = set()
        for step in seq["steps"]:
            for robot, joints in step["joints"].items():
                if robot not in self.arms:
                    raise RobotError(f"O robô '{robot}' da sequência não existe nesta configuração")
                check_joints(joints)
                involved.add(robot)
        for robot in involved:
            arm = self.arms[robot]
            if not arm.connected:
                raise Conflict(f"{robot} não está conectado")
            if arm.busy:
                raise Conflict(f"{robot} ainda está em movimento")
        self.cancel.clear()
        self.playback = {"name": name, "step": 0, "total": len(seq["steps"])}
        threading.Thread(target=self._play, args=(name, seq["steps"], speed), daemon=True).start()
        return {}

    def _play(self, name, steps, speed):
        self.log("info", f"Executando '{name}' ({len(steps)} pontos) a {speed:g} mm/s")
        i = 0
        try:
            for i, step in enumerate(steps, start=1):
                if self.cancel.is_set():
                    self.log("info", f"'{name}' parada antes do ponto {i}")
                    return
                self.playback["step"] = i
                request = {"mode": "joints", "speed": speed, "values": step["joints"]}
                errors = self.start_move(list(step["joints"]), request, wait=True,
                                         from_sequence=True)
                if errors:
                    raise RobotError("; ".join(f"{n}: {e}" for n, e in errors.items()))
            self.log("ok", f"Sequência '{name}' concluída")
        except (RobotError, Conflict) as err:
            self.log("error", f"'{name}' interrompida no ponto {i}: {err}")
        finally:
            self.playback = None

    # ---------- estado para a página ----------

    def state(self):
        with self.seq_lock:
            sequences = [{"name": n, "steps": s["steps"]} for n, s in self.sequences.items()]
            recording = dict(self.recording) if self.recording else None
        return {
            "robots": [arm.snapshot() for arm in self.arms.values()],
            "limits": {"joints": JOINT_LIMITS,
                       "box": {"min": BOX_MIN, "max": BOX_MAX},
                       "speed": [MIN_TCP_SPEED, MAX_TCP_SPEED]},
            "sequences": sequences,
            "recording": recording,
            "playback": dict(self.playback) if self.playback else None,
            "log": list(self.events),
        }


def parse_move(req, names):
    """Converte o JSON recebido em um pedido validado. Levanta RobotError se for inválido."""
    try:
        mode = req["mode"]
        speed = check_speed(float(req.get("speed", 20)))
        if mode == "home":
            return {"mode": mode, "speed": speed}
        if mode == "relative":
            delta = [float(v) for v in req["delta"]]
            if len(delta) != 3:
                raise RobotError("delta precisa de 3 valores (dx, dy, dz)")
            return {"mode": mode, "speed": speed, "delta": delta}
        if mode == "joint_jog":
            joint = int(req["joint"])
            if not 1 <= joint <= 6:
                raise RobotError("Junta deve ser de 1 a 6")
            return {"mode": mode, "speed": speed, "joint": joint, "delta": float(req["delta"])}
        if mode in ("linear", "joints"):
            values = {n: [float(v) for v in req["values"][n]] for n in names}
            if any(len(v) != 6 for v in values.values()):
                raise RobotError("São necessários 6 valores por robô")
            return {"mode": mode, "speed": speed, "values": values}
    except (KeyError, TypeError, ValueError):
        raise RobotError("Requisição de movimento inválida") from None
    raise RobotError(f"Modo de movimento desconhecido: {req.get('mode')!r}")


def parse_jog(req):
    """Pedido de jog contínuo -> (modo, deflexões de -1 a 1, velocidade do painel em mm/s)."""
    try:
        mode = req["mode"]
        vel = [max(-1.0, min(1.0, float(v))) for v in req["vel"]]
        speed = check_speed(float(req.get("speed", 20)))
    except (KeyError, TypeError, ValueError):
        raise RobotError("Requisição de jog inválida") from None
    if (mode, len(vel)) in (("joints", 6), ("cart", 3)):
        return mode, vel, speed
    raise RobotError("Jog: use mode 'joints' com 6 valores ou 'cart' com 3")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    panel = None
    token = None

    def log_message(self, *args):
        pass  # o painel já registra os eventos que interessam

    def _send(self, status, body, content_type="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if self.token is None:
            return True
        sent = self.headers.get("X-Token", "")
        return hmac.compare_digest(sent.encode(), self.token.encode())

    def do_GET(self):
        path = self.path.partition("?")[0]  # ignora ?query (cache-busting, links com parâmetros)
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/state":
            if not self._authorized():
                return self._send(401, {"error": "Token inválido"})
            self._send(200, self.panel.state())
        else:
            self._send(404, {"error": "Não encontrado"})

    def do_POST(self):
        if not self._authorized():
            return self._send(401, {"error": "Token inválido"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length > MAX_BODY:
                return self._send(413, {"error": "Corpo grande demais"})
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._send(400, {"error": "JSON inválido"})

        extra = {}
        try:
            names = body.get("robots") or []
            if self.path == "/api/connect":
                self.panel.connect(names)
            elif self.path == "/api/disconnect":
                self.panel.disconnect(names)
            elif self.path == "/api/move":
                errors = self.panel.start_move(names, body, wait=bool(body.get("wait")))
                if errors:
                    return self._send(400, {"error": "; ".join(f"{n}: {e}" for n, e in errors.items())})
            elif self.path == "/api/jog":
                self.panel.jog(names, body)
            elif self.path == "/api/seq":
                extra = self.panel.seq_action(body)
            else:
                return self._send(404, {"error": "Não encontrado"})
        except RobotError as err:
            return self._send(400, {"error": str(err)})
        except Conflict as err:
            return self._send(409, {"error": str(err)})
        except OSError as err:  # arquivo em uso, disco cheio, rede: avisa em vez de soltar a conexão
            self.panel.log("error", f"Erro de arquivo ou rede: {err}")
            return self._send(500, {"error": f"Erro de arquivo ou rede: {err}"})
        except Exception as err:  # qualquer outro defeito também precisa de resposta
            traceback.print_exc()
            self.panel.log("error", f"Erro interno: {type(err).__name__}: {err}")
            return self._send(500, {"error": f"Erro interno do painel: {type(err).__name__}: {err}"})
        self._send(200, {"ok": True, **extra})


def lan_address():
    """IP desta máquina na LAN (o UDP connect não envia nenhum pacote)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def split_host_port(value):
    host, sep, port = value.partition(":")
    return host, int(port) if sep else DEFAULT_PORT


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--robot", action="append", metavar="NOME=IP[:PORTA]",
                        help=f"robô a controlar (repita para cada um; padrão {DEFAULT_ROBOTS})")
    parser.add_argument("--listen", default="0.0.0.0",
                        help="interface onde o painel escuta (padrão: toda a LAN)")
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--token", help="token de acesso (padrão: gera um aleatório)")
    parser.add_argument("--no-token", action="store_true",
                        help="não exige token (qualquer um na rede move os robôs!)")
    parser.add_argument("--poll", type=float, default=0.5, help="segundos entre leituras de pose")
    parser.add_argument("--seq-file", default=DEFAULT_SEQ_FILE,
                        help="arquivo onde as sequências gravadas ficam salvas")
    parser.add_argument("--open", action="store_true",
                        help="abre o painel no navegador deste PC (já com o token)")
    args = parser.parse_args()

    robots = {name: split_host_port(addr)
              for name, addr in parse_robot_args(args.robot or DEFAULT_ROBOTS).items()}
    panel = Panel(robots, args.poll, args.seq_file)
    threading.Thread(target=panel.poll_loop, daemon=True).start()

    Handler.panel = panel
    Handler.token = None if args.no_token else (args.token or secrets.token_urlsafe(6))
    server = ThreadingHTTPServer((args.listen, args.http_port), Handler)
    server.daemon_threads = True

    url = f"http://{lan_address()}:{args.http_port}/"
    print("Robôs:", ", ".join(f"{n}={h}:{p}" for n, (h, p) in robots.items()))
    if Handler.token:
        print(f"\nAbra no navegador (qualquer dispositivo da LAN):\n  {url}#token={Handler.token}")
    else:
        print(f"\nABERTO SEM TOKEN. Endereço:\n  {url}")
    print("\nParada de emergência: só pelo botão físico / FlexPendant. Ctrl+C encerra o painel.")

    if args.open:
        # localhost (e não o IP da LAN): o navegador só libera o controle de Xbox nesse endereço
        local = f"http://localhost:{args.http_port}/" + (f"#token={Handler.token}" if Handler.token else "")
        threading.Timer(1.0, webbrowser.open, args=(local,)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        for arm in panel.arms.values():
            arm.client.close()


if __name__ == "__main__":
    main()
