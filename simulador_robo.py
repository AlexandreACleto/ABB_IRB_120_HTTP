"""Simulador do PyServer.mod: responde ao mesmo protocolo, sem precisar do robô.

Serve para testar o painel (painel_robos.py) sem hardware. Cada --port é um "robô".
O movimento é simulado com uma espera proporcional à distância. Atenção: juntas e pose
são independentes aqui (não há cinemática), então MOVEJ não altera POS e vice-versa.

Uso:
    python simulador_robo.py --port 1025 --port 1026
    python painel_robos.py --robot esq=127.0.0.1:1025 --robot dir=127.0.0.1:1026
"""
import argparse
import math
import socketserver
import threading
import time

# Mesmos limites do PyServer.mod
MIN_TCP_SPEED = 5
MAX_TCP_SPEED = 250
BOX_MIN = (-580, -580, 50)
BOX_MAX = (580, 580, 900)
JOINT_MIN = (-165, -110, -110, -160, -120, -400)
JOINT_MAX = (165, 110, 70, 160, 120, 400)
REACH_MM = 580
SHOULDER_Z = 290  # altura do eixo 2 do IRB 120


class SimRobot:
    def __init__(self, time_scale):
        self.time_scale = time_scale
        self.busy_until = 0.0  # quando o robô termina tudo o que já foi pedido (relógio monotônico)
        self.joints = [0.0, 0.0, 0.0, 0.0, 30.0, 0.0]
        self.pose = [374.0, 0.0, 630.0, 180.0, 0.0, 180.0]

    def execute(self, message):
        """Executa uma mensagem do cliente e devolve a resposta no formato do RAPID."""
        parts = message.split()
        if not parts:
            return "ERR PARSE"
        cmd = parts[0]
        try:
            args = [float(p) for p in parts[1:]]
        except ValueError:
            return "ERR PARSE"
        if len(args) > 8:  # o PyServer.mod aceita no máximo 8 números
            return "ERR PARSE"

        if cmd == "PING":
            return "OK PONG"
        if cmd == "POS":
            return "OK " + " ".join(f"{v:.1f}" for v in self.pose)
        if cmd == "JOINTS":
            return "OK " + " ".join(f"{v:.2f}" for v in self.joints)
        if cmd == "MOVEJ":
            return self._move_joints(args)
        if cmd == "MOVEL":
            return self._move_linear(args)
        return "ERR UNKNOWN"

    def _speed(self, v):
        return min(max(v, MIN_TCP_SPEED), MAX_TCP_SPEED)

    def _move_joints(self, args):
        if len(args) not in (7, 8):
            return "ERR ARGS"
        target, speed = args[:6], self._speed(args[6])
        zone = args[7] if len(args) == 8 else 0
        for i, value in enumerate(target):
            if not JOINT_MIN[i] <= value <= JOINT_MAX[i]:
                return f"ERR JOINT_LIMIT {i + 1}"
        delta = max(abs(t - c) for t, c in zip(target, self.joints))
        if zone > 0:
            seconds = math.radians(delta) * 400 / speed  # jog: velocidade física (TCP a ~400 mm do eixo 1)
        else:
            seconds = delta / (speed * 2)                # ~2 graus/s por mm/s, só para dar ritmo
        self._motion(seconds, zone)
        self.joints = list(target)
        return "OK DONE"

    def _move_linear(self, args):
        if len(args) not in (7, 8):
            return "ERR ARGS"
        target, speed = args[:6], self._speed(args[6])
        zone = args[7] if len(args) == 8 else 0
        for value, low, high in zip(target[:3], BOX_MIN, BOX_MAX):
            if not low <= value <= high:
                return "ERR WORKSPACE"
        x, y, z = target[:3]
        if math.sqrt(x * x + y * y + (z - SHOULDER_Z) ** 2) > REACH_MM + 100:
            return "ERR UNREACHABLE"
        distance = math.dist(target[:3], self.pose[:3])
        self._motion(distance / speed, zone)
        self.pose = list(target)
        return "OK DONE"

    def _motion(self, seconds, zone):
        """Relógio do robô: os movimentos entram numa fila e cada um começa quando o anterior termina.

        Sem raio de curva (fine) a resposta só sai quando o robô para no ponto. Com raio de curva ela
        sai ao entrar na curva (~30% antes do fim) e o próximo movimento emenda, como no robô real.
        """
        now = time.monotonic()
        duration = (seconds if zone > 0 else max(0.2, seconds)) * self.time_scale
        start = max(now, self.busy_until)
        self.busy_until = start + duration
        reply_at = self.busy_until - (0.3 * duration if zone > 0 else 0.0)
        time.sleep(max(0.0, reply_at - now))


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        print(f"[{self.server.server_address[1]}] cliente conectado: {self.client_address[0]}")
        while True:
            try:
                data = self.request.recv(1024)
            except OSError:
                break
            if not data:
                break
            message = data.decode("ascii", errors="replace").strip()
            reply = self.server.robot.execute(message)
            print(f"[{self.server.server_address[1]}] {message}  ->  {reply}")
            self.request.sendall(reply.encode("ascii"))
        print(f"[{self.server.server_address[1]}] cliente desconectou")


class Server(socketserver.TCPServer):
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, action="append",
                        help="porta de um robô simulado (repita para vários; padrão 1025)")
    parser.add_argument("--time-scale", type=float, default=1.0,
                        help="multiplica a duração dos movimentos (0.2 = 5x mais rápido)")
    args = parser.parse_args()

    # Um servidor serial por porta, como o PyServer.mod: atende um cliente por vez
    servers = []
    for port in args.port or [1025]:
        server = Server((args.host, port), Handler)
        server.robot = SimRobot(args.time_scale)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        print(f"Robô simulado em {args.host}:{port}")

    try:
        # Com timeout o Ctrl-C funciona no Windows (wait() sem timeout não é interrompível)
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        for server in servers:
            server.shutdown()


if __name__ == "__main__":
    main()
