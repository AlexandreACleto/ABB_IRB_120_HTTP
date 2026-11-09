"""Conexão somente-leitura com a controladora IRC5 (IRB 120) via PC SDK da ABB.

Lê versão do RobotWare, opções instaladas, estado e posição atual.
Não pede mastership e não escreve nada no robô.
"""
import argparse
import sys

# DLLs do PC SDK que vêm junto com o RobotStudio (build .NET Framework 4.8)
SDK_DIR = r"F:\studiioRobot\Bin-net48"
CONTROLLER_IP = "192.168.125.1"

# pythonnet usa o .NET Framework por padrão no Windows, compatível com Bin-net48
sys.path.append(SDK_DIR)
import clr  # noqa: E402

clr.AddReference("ABB.Robotics.Controllers.PC")

from ABB.Robotics.Controllers import (  # noqa: E402
    ConnectionType, Controller, ControllerFactory, UserInfo,
)
from ABB.Robotics.Controllers.Discovery import NetworkScanner  # noqa: E402
from ABB.Robotics.Controllers.MotionDomain import CoordinateSystemType  # noqa: E402


def find_controller_info(ip_address):
    """Varre a rede e devolve o ControllerInfo com o IP pedido."""
    scanner = NetworkScanner()
    scanner.Scan()
    for info in scanner.Controllers:
        if str(info.IPAddress) == ip_address:
            return info
    raise RuntimeError(f"Controladora {ip_address} não encontrada na rede")


def connect(info):
    """Cria o objeto Controller; a API mudou de nome entre versões do SDK."""
    try:
        return ControllerFactory.CreateFrom(info)
    except Exception:
        return Controller.Connect(info, ConnectionType.Standalone)


def main():
    parser = argparse.ArgumentParser(description="Lê dados da controladora (somente leitura)")
    parser.add_argument("--host", default=CONTROLLER_IP, help="IP da controladora")
    args = parser.parse_args()

    info = find_controller_info(args.host)
    print(f"Encontrada: {info.ControllerName} ({info.IPAddress}) RW {info.Version}")

    ctrl = connect(info)
    ctrl.Logon(UserInfo.DefaultUser)
    try:
        print(f"RobotWare : {ctrl.RobotWare.Version}")
        print(f"Modo      : {ctrl.OperatingMode}")
        print(f"Estado    : {ctrl.State}")

        print("Opções instaladas:")
        for option in ctrl.RobotWare.Options:
            print(f"  - {option.Description}")

        unit = ctrl.MotionSystem.ActiveMechanicalUnit
        joints = unit.GetPosition()
        rax = joints.RobAx
        print("Juntas (graus): "
              f"{rax.Rax_1:.2f}, {rax.Rax_2:.2f}, {rax.Rax_3:.2f}, "
              f"{rax.Rax_4:.2f}, {rax.Rax_5:.2f}, {rax.Rax_6:.2f}")

        tcp = unit.GetPosition(CoordinateSystemType.World)
        print(f"TCP (mm)  : X={tcp.Trans.X:.1f} Y={tcp.Trans.Y:.1f} Z={tcp.Trans.Z:.1f}")
    finally:
        ctrl.Logoff()
        ctrl.Dispose()


if __name__ == "__main__":
    main()
