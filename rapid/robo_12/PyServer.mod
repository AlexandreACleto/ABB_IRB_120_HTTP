MODULE PyServer
    ! Servidor TCP para comandar o IRB 120 a partir do Python (RobotWare 5.14).
    ! Requer a opcao 616-1 PC Interface (Socket Messaging).
    !
    ! Protocolo: uma mensagem de texto por comando (max 80 caracteres).
    ! Resposta sempre comeca com "OK" ou "ERR".
    !   PING                          -> OK PONG
    !   POS                           -> OK x y z rx ry rz   (mm, graus, tool0/wobj0)
    !   JOINTS                        -> OK j1 j2 j3 j4 j5 j6 (graus)
    !   MOVEJ j1 j2 j3 j4 j5 j6 v     -> OK DONE (apos parar no ponto)
    !   MOVEL x y z rx ry rz v        -> OK DONE (apos parar no ponto)
    !   MOVEJ ... v z / MOVEL ... v z -> com raio de curva z (mm, > 0): o robo NAO para no ponto,
    !                                    emenda no proximo movimento (jog continuo do painel).
    !                                    "OK DONE" volta quando o robo entra na curva, nao no ponto.

    ! IP da porta onde o PC esta ligado:
    !   porta Service       -> "192.168.125.1"
    !   porta LAN (switch)  -> IP fixo DESTE robo, ex. "192.168.0.11" (diferente em cada robo)
    !   controlador virtual -> "127.0.0.1"
    CONST string SERVER_IP := "192.168.0.12";
    CONST num SERVER_PORT := 1025;

    ! Limites de seguranca (conferidos de novo aqui, mesmo que o Python ja confira)
    CONST num MIN_TCP_SPEED := 5;
    CONST num MAX_TCP_SPEED := 250;
    CONST num MAX_ZONE := 30;
    CONST pos BOX_MIN := [-580, -580, 50];
    CONST pos BOX_MAX := [580, 580, 900];
    CONST num JOINT_MIN{6} := [-165, -110, -110, -160, -120, -400];
    CONST num JOINT_MAX{6} := [165, 110, 70, 160, 120, 400];

    VAR socketdev server_socket;
    VAR socketdev client_socket;
    VAR string rx_msg;
    VAR string cmd;
    VAR num args{8};
    VAR num nargs;

    ! TRUE enquanto os movimentos chegam encadeados (jog continuo, com raio de curva)
    VAR bool streaming := FALSE;

    ! Sem checagem de configuracao (o robo usa a solucao mais proxima da atual) e com o punho
    ! autorizado a passar perto de singularidade. Evita o erro 50143 "Configuracao dos eixos do robo".
    ! A checagem volta a ligar quando o programa e parado e reiniciado, por isso isto e repetido
    ! antes de cada movimento com parada (e ao entrar no jog continuo), nao so no inicio.
    PROC SetupMotion()
        ConfJ \Off;
        ConfL \Off;
        SingArea \Wrist;
    ENDPROC

    ! Diferenca entre dois angulos em graus, de 0 a 180 (trata a volta de 360)
    FUNC num AngDiff(num a, num b)
        VAR num d;
        d := a - b;
        WHILE d > 180 DO
            d := d - 360;
        ENDWHILE
        WHILE d < -180 DO
            d := d + 360;
        ENDWHILE
        RETURN Abs(d);
    ENDFUNC

    ! Nao se chama "main" para nao conflitar com o MainModule ja existente no robo.
    ! Para rodar: FlexPendant -> Program Editor -> Debug -> PP to Routine -> PyMain
    PROC PyMain()
        streaming := FALSE;
        SetupMotion;
        StartServer;
        WHILE TRUE DO
            rx_msg := "";
            SocketReceive client_socket \Str:=rx_msg \Time:=WAIT_MAX;
            HandleCommand rx_msg;
        ENDWHILE
    ERROR
        IF ERRNO = ERR_SOCK_CLOSED THEN
            ! Cliente desconectou: espera o proximo. TRYNEXT pula a instrucao que falhou,
            ! e rx_msg vazio garante que nenhum comando antigo seja repetido.
            SocketClose client_socket;
            rx_msg := "";
            WaitForClient;
            TRYNEXT;
        ENDIF
    ENDPROC

    PROC StartServer()
        ! Fecha sockets de uma execucao anterior (sem erro se ja estiverem fechados)
        SocketClose client_socket;
        SocketClose server_socket;
        SocketCreate server_socket;
        SocketBind server_socket, SERVER_IP, SERVER_PORT;
        SocketListen server_socket;
        WaitForClient;
    ENDPROC

    PROC WaitForClient()
        TPWrite "PyServer: aguardando cliente na porta " \Num:=SERVER_PORT;
        SocketAccept server_socket, client_socket \Time:=WAIT_MAX;
        TPWrite "PyServer: cliente conectado";
    ENDPROC

    PROC HandleCommand(string msg)
        IF StrLen(msg) = 0 THEN
            RETURN;
        ENDIF
        IF NOT ParseMessage(msg) THEN
            Reply "ERR PARSE";
            RETURN;
        ENDIF
        TEST cmd
        CASE "PING":
            Reply "OK PONG";
        CASE "POS":
            ReplyPosition;
        CASE "JOINTS":
            ReplyJoints;
        CASE "MOVEJ":
            DoMoveJ;
        CASE "MOVEL":
            DoMoveL;
        DEFAULT:
            Reply "ERR UNKNOWN";
        ENDTEST
    ENDPROC

    ! Separa a mensagem em cmd + ate 8 numeros (args{1..nargs})
    FUNC bool ParseMessage(string msg)
        VAR num pos := 1;
        VAR num next;
        VAR string token;

        nargs := 0;
        next := StrFind(msg, pos, " ");
        cmd := StrPart(msg, pos, next - pos);
        pos := next + 1;
        WHILE pos <= StrLen(msg) DO
            next := StrFind(msg, pos, " ");
            token := StrPart(msg, pos, next - pos);
            IF StrLen(token) > 0 THEN
                IF nargs >= 8 THEN
                    RETURN FALSE;
                ENDIF
                Incr nargs;
                IF NOT StrToVal(token, args{nargs}) THEN
                    RETURN FALSE;
                ENDIF
            ENDIF
            pos := next + 1;
        ENDWHILE
        RETURN TRUE;
    ENDFUNC

    PROC Reply(string text)
        SocketSend client_socket \Str:=text;
    ENDPROC

    ! Limita a velocidade pedida entre MIN_TCP_SPEED e MAX_TCP_SPEED
    FUNC speeddata MakeSpeed(num v_tcp)
        VAR num v;
        v := v_tcp;
        IF v < MIN_TCP_SPEED THEN
            v := MIN_TCP_SPEED;
        ENDIF
        IF v > MAX_TCP_SPEED THEN
            v := MAX_TCP_SPEED;
        ENDIF
        RETURN [v, 500, 5000, 1000];
    ENDFUNC

    ! Velocidade do jog continuo: alem do TCP, limita a reorientacao (graus/s) a metade de v,
    ! senao os eixos do punho (J4 a J6) andariam na velocidade maxima e o movimento ficaria
    ! "anda-para". So e usada junto com o raio de curva (movimentos encadeados).
    FUNC speeddata MakeJogSpeed(num v_tcp)
        VAR num v;
        v := v_tcp;
        IF v < MIN_TCP_SPEED THEN
            v := MIN_TCP_SPEED;
        ENDIF
        IF v > MAX_TCP_SPEED THEN
            v := MAX_TCP_SPEED;
        ENDIF
        RETURN [v, 0.5 * v, 5000, 1000];
    ENDFUNC

    ! Raio de curva (z, mm) no formato do zonedata padrao (como z10 = [FALSE,10,15,15,1.5,15,1.5])
    FUNC zonedata MakeZone(num z_mm)
        VAR num r;
        r := z_mm;
        IF r < 1 THEN
            r := 1;
        ENDIF
        IF r > MAX_ZONE THEN
            r := MAX_ZONE;
        ENDIF
        RETURN [FALSE, r, 1.5 * r, 1.5 * r, 0.15 * r, 1.5 * r, 0.15 * r];
    ENDFUNC

    PROC ReplyPosition()
        VAR robtarget p;
        p := CRobT(\Tool:=tool0 \WObj:=wobj0);
        Reply "OK " + NumToStr(p.trans.x, 1) + " " + NumToStr(p.trans.y, 1) + " " + NumToStr(p.trans.z, 1)
            + " " + NumToStr(EulerZYX(\X, p.rot), 1) + " " + NumToStr(EulerZYX(\Y, p.rot), 1)
            + " " + NumToStr(EulerZYX(\Z, p.rot), 1);
    ENDPROC

    PROC ReplyJoints()
        VAR jointtarget j;
        j := CJointT();
        Reply "OK " + NumToStr(j.robax.rax_1, 2) + " " + NumToStr(j.robax.rax_2, 2) + " " + NumToStr(j.robax.rax_3, 2)
            + " " + NumToStr(j.robax.rax_4, 2) + " " + NumToStr(j.robax.rax_5, 2) + " " + NumToStr(j.robax.rax_6, 2);
    ENDPROC

    PROC DoMoveJ()
        VAR jointtarget target;
        VAR speeddata spd;
        VAR zonedata zd;
        IF nargs <> 7 AND nargs <> 8 THEN
            Reply "ERR ARGS";
            RETURN;
        ENDIF
        FOR i FROM 1 TO 6 DO
            IF args{i} < JOINT_MIN{i} OR args{i} > JOINT_MAX{i} THEN
                Reply "ERR JOINT_LIMIT " + NumToStr(i, 0);
                RETURN;
            ENDIF
        ENDFOR
        ! Parte da posicao atual para manter os eixos externos
        target := CJointT();
        target.robax := [args{1}, args{2}, args{3}, args{4}, args{5}, args{6}];
        IF nargs = 8 AND args{8} > 0 THEN
            ! Jog continuo: com raio de curva o robo emenda no proximo passo sem parar
            IF NOT streaming THEN
                SetupMotion;
                streaming := TRUE;
            ENDIF
            spd := MakeJogSpeed(args{7});
            zd := MakeZone(args{8});
            MoveAbsJ target, spd, zd, tool0;
        ELSE
            SetupMotion;
            streaming := FALSE;
            MoveAbsJ target, MakeSpeed(args{7}), fine, tool0;
        ENDIF
        Reply "OK DONE";
    ENDPROC

    PROC DoMoveL()
        VAR robtarget target;
        VAR jointtarget reach_check;
        VAR speeddata spd;
        VAR zonedata zd;
        VAR num cur_x;
        VAR num cur_y;
        VAR num cur_z;
        IF nargs <> 7 AND nargs <> 8 THEN
            Reply "ERR ARGS";
            RETURN;
        ENDIF
        IF args{1} < BOX_MIN.x OR args{1} > BOX_MAX.x
            OR args{2} < BOX_MIN.y OR args{2} > BOX_MAX.y
            OR args{3} < BOX_MIN.z OR args{3} > BOX_MAX.z THEN
            Reply "ERR WORKSPACE";
            RETURN;
        ENDIF
        ! Parte da posicao atual para herdar configuracao e eixos externos
        target := CRobT(\Tool:=tool0 \WObj:=wobj0);
        target.trans := [args{1}, args{2}, args{3}];
        ! Se a orientacao pedida e a atual (dentro de 0,3 grau), nao reorienta. Os angulos chegam
        ! arredondados a 0,1 grau e, perto de uma singularidade do punho, esse arredondamento viraria
        ! uma rotacao grande dos eixos 4 a 6 (erro 50143, eixo 5).
        cur_x := EulerZYX(\X, target.rot);
        cur_y := EulerZYX(\Y, target.rot);
        cur_z := EulerZYX(\Z, target.rot);
        IF AngDiff(args{4}, cur_x) > 0.3 OR AngDiff(args{5}, cur_y) > 0.3 OR AngDiff(args{6}, cur_z) > 0.3 THEN
            target.rot := OrientZYX(args{6}, args{5}, args{4});
        ENDIF
        ! CalcJointT gera ERR_ROBLIMIT se o ponto estiver fora do alcance
        reach_check := CalcJointT(target, tool0 \WObj:=wobj0);
        IF nargs = 8 AND args{8} > 0 THEN
            ! Jog continuo: com raio de curva o robo emenda no proximo passo sem parar
            IF NOT streaming THEN
                SetupMotion;
                streaming := TRUE;
            ENDIF
            spd := MakeJogSpeed(args{7});
            zd := MakeZone(args{8});
            MoveL target, spd, zd, tool0 \WObj:=wobj0;
        ELSE
            SetupMotion;
            streaming := FALSE;
            MoveL target, MakeSpeed(args{7}), fine, tool0 \WObj:=wobj0;
        ENDIF
        Reply "OK DONE";
    ERROR
        IF ERRNO = ERR_ROBLIMIT THEN
            Reply "ERR UNREACHABLE";
            RETURN;
        ENDIF
    ENDPROC
ENDMODULE
