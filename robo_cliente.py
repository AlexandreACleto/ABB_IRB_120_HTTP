"""Cliente Python para o servidor RAPID PyServer.mod (IRB 120 / IRC5).

Uso como biblioteca:
    with IRB120Client() as robot:
        print(robot.get_pose())
        robot.move_linear(400, 0, 500, 180, 0, 180, speed=50)

Uso pela linha de comando (demo: sobe 20 mm em Z e volta):
    python robo_cliente.py --demo
    python robo_cliente.py --host 127.0.0.1 --demo   # controlador virtual
"""
import argparse
import socket

DEFAULT_HOST = "192.168.125.1"
DEFAULT_PORT = 1025

# RAPID só recebe strings de até 80 caracteres
MAX_MESSAGE_LEN = 80

# Mesmos limites do PyServer.mod: conferidos aqui para falhar antes de enviar
MAX_TCP_SPEED = 250.0
MIN_TCP_SPEED = 5.0
MAX_ZONE = 30.0  # raio de curva máximo (mm), igual ao MAX_ZONE do PyServer.mod
REACH_MM = 580.0
BOX_MIN = (-580.0, -580.0, 50.0)
BOX_MAX = (580.0, 580.0, 900.0)
JOINT_LIMITS = [(-165, 165), (-110, 110), (-110, 70),
                (-160, 160), (-120, 120), (-400, 400)]


class RobotError(Exception):
    """Erro devolvido pelo robô ("ERR ...") ou recusado pelo cliente."""


class IRB120Client:
    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, move_timeout=120.0):
        self.host = host
        self.port = port
        self.move_timeout = move_timeout
        self.sock = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port), timeout=5.0)
        self.ping()

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def _request(self, message, timeout=5.0):
        """Envia um comando e devolve os campos da resposta 'OK ...'."""
        if len(message) > MAX_MESSAGE_LEN:
            raise RobotError(f"Mensagem com {len(message)} caracteres (máx {MAX_MESSAGE_LEN})")
        self.sock.settimeout(timeout)
        self.sock.sendall(message.encode("ascii"))
        reply = self.sock.recv(1024).decode("ascii").strip()
        if not reply:
            raise ConnectionError("Servidor fechou a conexão")
        if not reply.startswith("OK"):
            raise RobotError(f"{message.split()[0]}: {reply}")
        return reply.split()[1:]

    def ping(self):
        return self._request("PING") == ["PONG"]

    def get_pose(self):
        """(x, y, z, rx, ry, rz) do tool0 na base World, em mm e graus."""
        return tuple(float(v) for v in self._request("POS"))

    def get_joints(self):
        """(j1..j6) em graus."""
        return tuple(float(v) for v in self._request("JOINTS"))

    def move_joints(self, joints, speed=50, zone=0):
        """Move para os ângulos de junta (graus) e espera o robô parar.

        Com zone > 0 (mm) o robô não para no ponto: emenda no próximo movimento (jog contínuo),
        e a chamada volta quando ele entra na curva, não quando chega ao ponto.
        """
        check_joints(joints)
        speed = check_speed(speed)
        values = " ".join(f"{a:.2f}" for a in joints)
        self._request(f"MOVEJ {values} {speed:.1f}{_zone_arg(zone)}", timeout=self.move_timeout)

    def move_linear(self, x, y, z, rx, ry, rz, speed=50, zone=0):
        """Move o TCP em linha reta até (x, y, z) mm com orientação Euler ZYX (graus).

        zone: veja move_joints.
        """
        check_linear_target(x, y, z)
        speed = check_speed(speed)
        values = " ".join(f"{v:.1f}" for v in (x, y, z, rx, ry, rz))
        self._request(f"MOVEL {values} {speed:.1f}{_zone_arg(zone)}", timeout=self.move_timeout)

    def move_relative(self, dx=0.0, dy=0.0, dz=0.0, speed=50):
        """Desloca o TCP a partir da posição atual, mantendo a orientação."""
        x, y, z, rx, ry, rz = self.get_pose()
        self.move_linear(x + dx, y + dy, z + dz, rx, ry, rz, speed=speed)


def _zone_arg(zone):
    """Texto do raio de curva no fim da mensagem ('' = parada exata, como antes)."""
    if not 0 <= zone <= MAX_ZONE:
        raise RobotError(f"Raio de curva {zone} fora de [0, {MAX_ZONE}] mm")
    return f" {zone:.1f}" if zone > 0 else ""


def check_speed(speed):
    if not MIN_TCP_SPEED <= speed <= MAX_TCP_SPEED:
        raise RobotError(f"Velocidade {speed} fora de [{MIN_TCP_SPEED}, {MAX_TCP_SPEED}] mm/s")
    return speed


def check_joints(joints):
    """Levanta RobotError se os 6 ângulos (graus) não couberem nos limites das juntas."""
    if len(joints) != 6:
        raise RobotError("São necessários 6 ângulos")
    for i, (angle, (low, high)) in enumerate(zip(joints, JOINT_LIMITS), start=1):
        if not low <= angle <= high:
            raise RobotError(f"J{i}={angle} fora do limite [{low}, {high}]")


def check_linear_target(x, y, z):
    """Levanta RobotError se (x, y, z) mm sair da caixa de trabalho ou do alcance."""
    for value, low, high, axis in zip((x, y, z), BOX_MIN, BOX_MAX, "XYZ"):
        if not low <= value <= high:
            raise RobotError(f"{axis}={value} fora da caixa de trabalho [{low}, {high}]")
    # Distância do eixo 1 no plano XY: aproximação grosseira do alcance
    if (x * x + y * y) ** 0.5 > REACH_MM:
        raise RobotError(f"Ponto a mais de {REACH_MM} mm do eixo 1")


def demo(host, port):
    with IRB120Client(host, port) as robot:
        print("Conectado.")
        print("Juntas:", robot.get_joints())
        start = robot.get_pose()
        print("Pose  :", start)

        input("\nO robô vai SUBIR 20 mm e VOLTAR a 20 mm/s. Enter para continuar, Ctrl+C cancela...")
        robot.move_relative(dz=20, speed=20)
        print("Subiu :", robot.get_pose())
        robot.move_linear(*start, speed=20)
        print("Voltou:", robot.get_pose())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--demo", action="store_true", help="sobe 20 mm em Z e volta")
    args = parser.parse_args()

    if args.demo:
        demo(args.host, args.port)
    else:
        with IRB120Client(args.host, args.port) as robot:
            print("Juntas:", robot.get_joints())
            print("Pose  :", robot.get_pose())
