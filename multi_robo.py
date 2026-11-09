"""Controla vários IRB 120 ao mesmo tempo (cada um rodando PyServer.mod).

Cada robô tem seu próprio IRB120Client; os comandos são disparados em paralelo
(uma thread por robô) e a chamada só retorna quando TODOS terminaram.

Uso como biblioteca:
    robots = {"esq": "192.168.0.11", "dir": "192.168.0.12"}
    with RobotFleet(robots) as fleet:
        print(fleet.get_poses())
        fleet.move_relative({"esq": (0, 0, 20), "dir": (0, 0, 20)}, speed=20)

Uso pela linha de comando:
    python multi_robo.py --robot esq=192.168.0.11 --robot dir=192.168.0.12
    python multi_robo.py --robot esq=192.168.0.11 --robot dir=192.168.0.12 --demo
"""
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor

from robo_cliente import DEFAULT_PORT, IRB120Client, RobotError


class FleetError(Exception):
    """Um ou mais robôs falharam; 'errors' mapeia nome -> exceção."""

    def __init__(self, errors):
        self.errors = errors
        detail = "; ".join(f"{name}: {err}" for name, err in errors.items())
        super().__init__(f"Falha em {len(errors)} robô(s): {detail}")


class RobotFleet:
    def __init__(self, robots, port=DEFAULT_PORT):
        """robots: dicionário {nome: ip}."""
        self.clients = {name: IRB120Client(ip, port) for name, ip in robots.items()}
        self.pool = ThreadPoolExecutor(max_workers=len(self.clients))

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        try:
            self._run_all({name: client.connect for name, client in self.clients.items()})
        except Exception:
            # Se um robô não conectar, fecha os que conectaram antes de propagar
            self.close()
            raise

    def close(self):
        for client in self.clients.values():
            client.close()
        self.pool.shutdown(wait=True)

    def _run_all(self, tasks):
        """Executa {nome: função} em paralelo e devolve {nome: resultado}.

        Uma barreira segura todas as threads até a última estar pronta, para que
        os comandos saiam para os robôs praticamente no mesmo instante.
        """
        barrier = threading.Barrier(len(tasks))

        def run(func):
            barrier.wait()
            return func()

        futures = {name: self.pool.submit(run, func) for name, func in tasks.items()}
        results, errors = {}, {}
        # Espera todos terminarem, mesmo se algum falhar, para nenhum robô ficar "solto"
        for name, future in futures.items():
            try:
                results[name] = future.result()
            except Exception as err:  # noqa: BLE001 - repassado em FleetError
                errors[name] = err
        if errors:
            raise FleetError(errors)
        return results

    def _client(self, name):
        if name not in self.clients:
            raise RobotError(f"Robô desconhecido: {name}")
        return self.clients[name]

    def get_poses(self):
        return self._run_all({n: c.get_pose for n, c in self.clients.items()})

    def get_joints(self):
        return self._run_all({n: c.get_joints for n, c in self.clients.items()})

    def move_linear(self, targets, speed=50):
        """targets: {nome: (x, y, z, rx, ry, rz)}. Robôs fora do dicionário ficam parados."""
        return self._run_all({
            name: (lambda c=self._client(name), t=target: c.move_linear(*t, speed=speed))
            for name, target in targets.items()
        })

    def move_joints(self, targets, speed=50):
        """targets: {nome: (j1, ..., j6)}."""
        return self._run_all({
            name: (lambda c=self._client(name), t=target: c.move_joints(t, speed=speed))
            for name, target in targets.items()
        })

    def move_relative(self, offsets, speed=50):
        """offsets: {nome: (dx, dy, dz)} em mm, mantendo a orientação."""
        return self._run_all({
            name: (lambda c=self._client(name), d=offset: c.move_relative(*d, speed=speed))
            for name, offset in offsets.items()
        })


def parse_robot_args(values):
    """Converte ['esq=192.168.0.11', ...] em {'esq': '192.168.0.11', ...}."""
    robots = {}
    for value in values:
        name, sep, ip = value.partition("=")
        if not sep or not name or not ip:
            raise SystemExit(f"--robot deve ser nome=ip, recebido: {value!r}")
        robots[name] = ip
    return robots


def print_poses(fleet, title):
    print(title)
    for name, pose in fleet.get_poses().items():
        print(f"  {name:>6}: " + " ".join(f"{v:8.1f}" for v in pose))


def demo(fleet):
    start = fleet.get_poses()
    print_poses(fleet, "Pose inicial:")
    input("\nTODOS os robôs vão SUBIR 20 mm e VOLTAR a 20 mm/s. Enter continua, Ctrl+C cancela...")
    fleet.move_relative({name: (0, 0, 20) for name in fleet.clients}, speed=20)
    print_poses(fleet, "Depois de subir:")
    fleet.move_linear(start, speed=20)
    print_poses(fleet, "Depois de voltar:")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--robot", action="append", required=True, metavar="NOME=IP",
                        help="robô a controlar (repita para cada um)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--demo", action="store_true", help="todos sobem 20 mm e voltam juntos")
    args = parser.parse_args()

    with RobotFleet(parse_robot_args(args.robot), args.port) as fleet:
        if args.demo:
            demo(fleet)
        else:
            print_poses(fleet, "Poses:")
