"""Monitor em tempo real do IRB 120 (somente leitura, via PC SDK).

Mostra modo, estado, execução RAPID, juntas e TCP atualizando no console.
Uso:
    python monitor_robo.py                 # roda até Ctrl+C
    python monitor_robo.py --host 192.168.0.12   # outro robô (porta LAN)
    python monitor_robo.py --hz 20         # taxa de atualização
    python monitor_robo.py --csv log.csv   # também grava cada amostra em CSV
"""
import argparse
import csv
import os
import time
from datetime import datetime

from conectar_robo import CONTROLLER_IP, connect, find_controller_info
from ABB.Robotics.Controllers import UserInfo
from ABB.Robotics.Controllers.MotionDomain import CoordinateSystemType

CSV_HEADER = ["timestamp", "mode", "state", "rapid",
              "j1", "j2", "j3", "j4", "j5", "j6",
              "x", "y", "z", "rx", "ry", "rz"]


def read_sample(ctrl):
    """Lê uma amostra completa do robô e devolve como dicionário."""
    unit = ctrl.MotionSystem.ActiveMechanicalUnit
    rax = unit.GetPosition().RobAx
    tcp = unit.GetPosition(CoordinateSystemType.World)
    # pythonnet devolve os parâmetros 'out' como tupla (rx, ry, rz)
    rx, ry, rz = tcp.Rot.ToEulerAngles(0.0, 0.0, 0.0)
    return {
        "timestamp": datetime.now().isoformat(timespec="milliseconds"),
        "mode": str(ctrl.OperatingMode),
        "state": str(ctrl.State),
        "rapid": str(ctrl.Rapid.ExecutionStatus),
        "j1": rax.Rax_1, "j2": rax.Rax_2, "j3": rax.Rax_3,
        "j4": rax.Rax_4, "j5": rax.Rax_5, "j6": rax.Rax_6,
        "x": tcp.Trans.X, "y": tcp.Trans.Y, "z": tcp.Trans.Z,
        "rx": rx, "ry": ry, "rz": rz,
    }


def render(sample, hz, host):
    """Redesenha a tela no mesmo lugar (código ANSI: cursor no topo + limpa)."""
    s = sample
    print("\x1b[H\x1b[J", end="")
    print(f"IRB 120 @ {host}   {s['timestamp']}   ({hz} Hz, Ctrl+C sai)\n")
    print(f"Modo   : {s['mode']}")
    print(f"Estado : {s['state']}")
    print(f"RAPID  : {s['rapid']}\n")
    print("Juntas (graus)")
    for i in range(1, 7):
        print(f"  J{i}: {s[f'j{i}']:9.2f}")
    print("\nTCP (mm / graus, base World)")
    print(f"  X: {s['x']:9.1f}   Y: {s['y']:9.1f}   Z: {s['z']:9.1f}")
    print(f"  Rx:{s['rx']:9.1f}   Ry:{s['ry']:9.1f}   Rz:{s['rz']:9.1f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=CONTROLLER_IP, help="IP da controladora")
    parser.add_argument("--hz", type=float, default=10, help="atualizações por segundo")
    parser.add_argument("--csv", help="arquivo CSV para gravar as amostras")
    parser.add_argument("--seconds", type=float, help="para sozinho após N segundos")
    args = parser.parse_args()

    os.system("")  # ativa códigos ANSI no console do Windows
    period = 1.0 / args.hz

    ctrl = connect(find_controller_info(args.host))
    ctrl.Logon(UserInfo.DefaultUser)
    csv_file = open(args.csv, "w", newline="", encoding="utf-8") if args.csv else None
    writer = csv.DictWriter(csv_file, fieldnames=CSV_HEADER) if csv_file else None
    if writer:
        writer.writeheader()

    start = time.monotonic()
    try:
        while args.seconds is None or time.monotonic() - start < args.seconds:
            tick = time.monotonic()
            sample = read_sample(ctrl)
            render(sample, args.hz, args.host)
            if writer:
                writer.writerow(sample)
            # dorme só o que sobrou do período, para manter a taxa estável
            time.sleep(max(0.0, period - (time.monotonic() - tick)))
    except KeyboardInterrupt:
        pass
    finally:
        if csv_file:
            csv_file.close()
        ctrl.Logoff()
        ctrl.Dispose()
        print("\nDesconectado.")


if __name__ == "__main__":
    main()
