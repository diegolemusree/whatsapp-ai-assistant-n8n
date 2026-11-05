"""Acomoda los nodos de un workflow de n8n de izquierda a derecha, con el menor número de líneas cruzadas.

Reglas:
- Columnas por "camino más largo": cada nodo queda a la derecha de todos los que lo alimentan (nunca hay flechas hacia atrás).
- Filas por recorrido en orden de salidas: la primera salida sigue en la misma fila; las demás (ej. el "false" de un IF)
  bajan. Así las flechas que salen de un IF o Switch no se cruzan.
- Los sub-nodos de IA (modelo, memoria, parser) van justo debajo de su nodo; el siguiente nodo se recorre para dejarles espacio.
- Entre grupos distintos queda una columna o fila libre, para que sus cajas no se encimen.
- Las notas (sticky notes) van arriba a la izquierda.

Uso:
  python3 tools/n8n_acomodar.py <workflow.json> [--vista salida.png]   -> imprime posiciones y, opcional, genera una vista previa
"""
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ANCHO_COL = 224
ALTO_FILA = 192
STICKY = "n8n-nodes-base.stickyNote"
ORDEN_AI = ["ai_languageModel", "ai_memory", "ai_tool", "ai_outputParser"]


def _estructura(wf):
    nodos = {n["name"]: n for n in wf["nodes"]}
    principales = defaultdict(list)  # origen -> [(salida, destino)]
    padres = defaultdict(list)       # destino -> [origen]
    ai_hijos = defaultdict(list)     # nodo -> [(tipo, sub-nodo)]
    es_sub = set()
    for origen, tipos in wf["connections"].items():
        for tipo, salidas in tipos.items():
            for i, destinos in enumerate(salidas):
                for d in destinos or []:
                    if tipo == "main":
                        principales[origen].append((i, d["node"]))
                        padres[d["node"]].append(origen)
                    elif tipo.startswith("ai_"):
                        ai_hijos[d["node"]].append((tipo, origen))
                        es_sub.add(origen)
    for k in ai_hijos:
        ai_hijos[k].sort(key=lambda t: (ORDEN_AI.index(t[0]) if t[0] in ORDEN_AI else 99, t[1]))
    grupo = {}
    por_id = {n["id"]: n["name"] for n in wf["nodes"]}
    for g in wf.get("nodeGroups") or []:
        for nid in g.get("nodeIds") or []:
            if nid in por_id:
                grupo[por_id[nid]] = g["name"]
        for nom in g.get("nodeNames") or []:
            grupo[nom] = g["name"]
    return nodos, principales, padres, ai_hijos, es_sub, grupo


def _huella(nombre, ai_hijos):
    """Ancho (columnas) y alto (filas) que ocupa un nodo con todos sus sub-nodos debajo."""
    hijos = ai_hijos.get(nombre, [])
    if not hijos:
        return 1, 1
    ancho, alto = 0, 0
    for _, h in hijos:
        w, a = _huella(h, ai_hijos)
        ancho += w
        alto = max(alto, a)
    return max(1, ancho), 1 + alto


def _colocar_subs(nombre, col, fila, ai_hijos, pos):
    c = col
    for _, h in ai_hijos.get(nombre, []):
        pos[h] = (c, fila + 1)
        w, _ = _huella(h, ai_hijos)
        _colocar_subs(h, c, fila + 1, ai_hijos, pos)
        c += w


def acomodar(wf):
    """Cambia las posiciones de los nodos de wf (en el lugar) y lo regresa."""
    nodos, principales, padres, ai_hijos, es_sub, grupo = _estructura(wf)
    main = [n for n in nodos if n not in es_sub and nodos[n]["type"] != STICKY]
    huella = {n: _huella(n, ai_hijos) for n in main}

    # 1) Columnas: camino más largo (orden topológico)
    grado = {n: 0 for n in main}
    for n in main:
        for _, d in principales.get(n, []):
            if d in grado:
                grado[d] += 1
    raices = [n for n in main if grado[n] == 0]
    raices.sort(key=lambda n: (nodos[n]["position"][1], nodos[n]["position"][0]))
    col = {n: 0 for n in main}
    cola = list(raices)
    pendientes = dict(grado)
    orden = []
    while cola:
        n = cola.pop(0)
        orden.append(n)
        for _, d in principales.get(n, []):
            if d not in pendientes:
                continue
            salto = 1 if grupo.get(n) != grupo.get(d) else 0
            col[d] = max(col[d], col[n] + huella[n][0] + salto)
            pendientes[d] -= 1
            if pendientes[d] == 0:
                cola.append(d)
    if len(orden) != len(main):
        raise RuntimeError("El workflow tiene un ciclo; no se puede acomodar automáticamente.")
    # Un nodo suelto (sin grupo) con un solo padre y un solo hijo que junta varias ramas se acerca a ese hijo:
    # así su línea larga no atraviesa todo el flujo (ej. "Respuesta - Solo Texto" junto a "Consolidar Respuesta").
    for n in reversed(orden):
        hijos = [d for _, d in principales.get(n, []) if d in col]
        if (grupo.get(n) is None and len(padres.get(n, [])) == 1 and len(set(hijos)) == 1
                and len(padres.get(hijos[0], [])) >= 2):
            d = hijos[0]
            col[n] = max(col[n], col[d] - huella[n][0] - (1 if grupo.get(n) != grupo.get(d) else 0))

    # 2) Filas: recorrido en profundidad por orden de salidas, empujando hacia abajo si hay choque
    ocupado = {}  # (col, fila) -> grupo (None si no tiene) o "__pasillo__"
    caja = {}     # grupo -> [col_min, col_max, fila_min, fila_max]: la caja que n8n dibuja alrededor del grupo
    fila = {}
    hacia_arriba = set()  # nodos de ramas que se colocaron arriba del camino principal

    def se_tocan(a, b, margen=1):
        return not (a[1] + margen < b[0] or b[1] + margen < a[0] or a[3] + margen < b[2] or b[3] + margen < a[2])

    def choca(c0, f0, w, h, g):
        rect = [c0, c0 + w - 1, f0, f0 + h - 1]
        for c in range(c0, c0 + w):
            for f in range(f0, f0 + h):
                if (c, f) in ocupado:
                    return True
        # El nodo no puede caer dentro (ni pegado) de la caja de otro grupo
        for g2, b in caja.items():
            if g2 != g and se_tocan(rect, b):
                return True
        # Si el nodo tiene grupo, su caja crecida no puede tocar otras cajas ni tragarse nodos ajenos
        if g is not None:
            b = caja.get(g, rect)
            nueva = [min(b[0], rect[0]), max(b[1], rect[1]), min(b[2], rect[2]), max(b[3], rect[3])]
            for g2, b2 in caja.items():
                if g2 != g and se_tocan(nueva, b2):
                    return True
            for (c, f), o in ocupado.items():
                if o != g and o != "__pasillo__" and nueva[0] <= c <= nueva[1] and nueva[2] <= f <= nueva[3]:
                    return True
        return False

    def ocupar(n, c0, f0):
        w, h = huella[n]
        g = grupo.get(n)
        for c in range(c0, c0 + w):
            for f in range(f0, f0 + h):
                ocupado[(c, f)] = g
        if g is not None:
            b = caja.get(g, [c0, c0 + w - 1, f0, f0 + h - 1])
            caja[g] = [min(b[0], c0), max(b[1], c0 + w - 1), min(b[2], f0), max(b[3], f0 + h - 1)]

    def colocar(n, deseada, padre=None):
        w, h = huella[n]
        f = deseada
        while choca(col[n], f, w, h, grupo.get(n)):
            f += 1
        fila[n] = f
        ocupar(n, col[n], f)
        if padre is not None and fila[padre] == f:
            for c in range(col[padre] + huella[padre][0], col[n]):
                ocupado.setdefault((c, f), "__pasillo__")

    def ultima_col(d):
        """Columna más a la derecha que ocupará la rama que empieza en d (solo nodos aún sin colocar)."""
        fin, pila, vistos = col[d] + huella[d][0] - 1, [d], {d}
        while pila:
            x = pila.pop()
            fin = max(fin, col[x] + huella[x][0] - 1)
            for _, y in principales.get(x, []):
                if y not in fila and y not in vistos:
                    vistos.add(y)
                    pila.append(y)
        return fin

    def alcance(x):
        """Nodos aún sin colocar a los que se llega desde x (incluido x)."""
        vistos, pila = {x}, [x]
        while pila:
            y = pila.pop()
            for _, z in principales.get(y, []):
                if z not in fila and z not in vistos:
                    vistos.add(z)
                    pila.append(z)
        return vistos

    def abajo(d, n):
        """Fila para una rama secundaria: franja completa debajo de todo lo que ya ocupa sus columnas."""
        fin = ultima_col(d)
        filas_usadas = [f for (c, f), o in ocupado.items() if col[d] <= c <= fin]
        debajo_de_cajas = [b[3] + 2 for g2, b in caja.items()
                           if g2 != grupo.get(d) and not (b[1] < col[d] or fin < b[0])]
        return max([fila[n] + 1] + [f + 1 for f in filas_usadas] + debajo_de_cajas)

    def fila_arriba(d, n):
        """Primera fila libre arriba de n donde cabe toda la rama d, o None si no cabe (p. ej. hay otra caja de grupo)."""
        fin = ultima_col(d)
        for f in range(fila[n] - 1, fila[n] - 4, -1):
            if not any((c, f) in ocupado for c in range(col[d], fin + 1)) and not choca(col[d], f, *huella[d], grupo.get(d)):
                return f
        return None

    def colocar_arriba(d, n):
        """Coloca la rama corta d arriba de la fila de n (el 'sí' de un IF que es solo una salida o un rodeo)."""
        f = fila_arriba(d, n)
        if f is not None:
            for x in alcance(d):
                hacia_arriba.add(x)  # sus propias ramas secundarias también suben, para no cruzar el camino principal
        colocar(d, f if f is not None else abajo(d, n), n)
        recorrer(d)

    def recorrer(n):
        hijos = sorted(principales.get(n, []), key=lambda t: t[0])
        sin_colocar = [(i, d) for i, d in hijos if d not in fila]
        salidas = {i for i, _ in sin_colocar}
        if len(sin_colocar) == 2 and salidas == {0, 1}:
            si, no = sin_colocar[0][1], sin_colocar[1][1]
            alcance_si, alcance_no = alcance(si), alcance(no)
            rodeo = no in alcance_si  # el "sí" da una vuelta y regresa al camino del "no"
            mas_corto = len(alcance_si - alcance_no) < len(alcance_no - alcance_si)
            if (rodeo or mas_corto) and fila_arriba(si, n) is not None:
                # El "no" sigue recto (camino principal) y el "sí", más corto, sube: sus flechas no se cruzan
                colocar(no, fila[n], n)
                recorrer(no)
                if si not in fila:
                    colocar_arriba(si, n)
                return
        primero = True
        for _, d in hijos:
            if d in fila:
                continue
            if primero:
                deseada = fila[n]
            elif n in hacia_arriba and fila_arriba(d, n) is not None:
                # Rama secundaria dentro de una franja que ya va arriba: sigue subiendo
                hacia_arriba.add(d)
                deseada = fila_arriba(d, n)
            else:
                # Rama secundaria: va completa como franja debajo de todo lo que ya ocupa sus columnas
                deseada = abajo(d, n)
            colocar(d, deseada, n)
            primero = False
            recorrer(d)

    siguiente = 0
    for r in raices:
        if r in fila:
            continue
        colocar(r, siguiente)
        recorrer(r)
        siguiente = fila[r] + 1

    # 3) Coordenadas
    pos = {}
    for n in main:
        pos[n] = (col[n], fila[n])
        _colocar_subs(n, col[n], fila[n], ai_hijos, pos)
    for n in nodos.values():
        if n["name"] in pos:
            c, f = pos[n["name"]]
            n["position"] = [c * ANCHO_COL, f * ALTO_FILA]
    xs = [n["position"][0] for n in nodos.values() if n["type"] != STICKY]
    ys = [n["position"][1] for n in nodos.values() if n["type"] != STICKY]
    y_nota = min(ys) - 96
    for n in nodos.values():
        if n["type"] == STICKY:
            alto = n.get("parameters", {}).get("height", 400)
            n["position"] = [min(xs), y_nota - alto]
            y_nota -= alto + 32
    return wf


def revisar(wf):
    """Cuenta líneas cruzadas (aprox. con segmentos rectos), nodos encimados y cajas de grupo encimadas."""
    nodos, principales, padres, ai_hijos, es_sub, grupo = _estructura(wf)
    P = {n: nodos[n]["position"] for n in nodos if nodos[n]["type"] != STICKY}
    seg = [((P[o][0] + 100, P[o][1] + 50), (P[d][0], P[d][1] + 50), o, d) for o, l in principales.items() for _, d in l]

    def cruza(a, b, c, d):
        def orient(p, q, r):
            return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return (orient(a, b, c) * orient(a, b, d) < 0) and (orient(c, d, a) * orient(c, d, b) < 0)

    cruces = sum(1 for i in range(len(seg)) for j in range(i + 1, len(seg))
                 if len({seg[i][2], seg[i][3], seg[j][2], seg[j][3]}) == 4 and cruza(seg[i][0], seg[i][1], seg[j][0], seg[j][1]))
    nombres = list(P)
    encimados = [(a, b) for i, a in enumerate(nombres) for b in nombres[i + 1:]
                 if abs(P[a][0] - P[b][0]) < 160 and abs(P[a][1] - P[b][1]) < 140]
    cajas = {}
    for n, g in grupo.items():
        if n in P:
            x, y = P[n]
            b = cajas.get(g, [x, x, y, y])
            cajas[g] = [min(b[0], x), max(b[1], x), min(b[2], y), max(b[3], y)]
    gs = list(cajas)
    cajas_encimadas = [(a, b) for i, a in enumerate(gs) for b in gs[i + 1:]
                       if not (cajas[a][1] + 140 < cajas[b][0] - 40 or cajas[b][1] + 140 < cajas[a][0] - 40 or
                               cajas[a][3] + 140 < cajas[b][2] - 64 or cajas[b][3] + 140 < cajas[a][2] - 64)]
    ajenos = [(n, g) for g, b in cajas.items() for n in P
              if grupo.get(n) != g and b[0] - 40 <= P[n][0] <= b[1] + 140 and b[2] - 64 <= P[n][1] <= b[3] + 140]
    return {"lineas_cruzadas": cruces, "nodos_encimados": encimados, "cajas_encimadas": cajas_encimadas,
            "nodos_dentro_de_otro_grupo": ajenos}


def vista_previa(wf, salida):
    """Dibuja el acomodo (nodos, grupos y flechas) en un PNG usando Chrome sin ventana."""
    nodos, principales, padres, ai_hijos, es_sub, grupo = _estructura(wf)
    P = {n: nodos[n]["position"] for n in nodos}
    xs = [p[0] for p in P.values()]
    ys = [p[1] for p in P.values()]
    x0, y0 = min(xs) - 120, min(ys) - 120
    W, H = max(xs) - x0 + 360, max(ys) - y0 + 320
    partes = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="{x0} {y0} {W} {H}" '
              f'style="background:#1e1e1e;font-family:Helvetica">']
    colores = ["#3b5bdb", "#2f9e44", "#e8590c", "#ae3ec9", "#1098ad", "#f08c00", "#c2255c", "#5c940d", "#4263eb", "#d6336c"]
    for i, g in enumerate(sorted(set(grupo.values()))):
        miembros = [n for n, gg in grupo.items() if gg == g and n in P]
        gx = min(P[n][0] for n in miembros) - 40
        gy = min(P[n][1] for n in miembros) - 64
        gw = max(P[n][0] for n in miembros) + 140 - gx
        gh = max(P[n][1] for n in miembros) + 140 - gy
        c = colores[i % len(colores)]
        partes.append(f'<rect x="{gx}" y="{gy}" width="{gw}" height="{gh}" rx="12" fill="{c}" fill-opacity="0.12" stroke="{c}"/>'
                      f'<text x="{gx + 12}" y="{gy + 26}" fill="{c}" font-size="20" font-weight="bold">{g}</text>')
    for o, lista in principales.items():
        for _, d in lista:
            (ax, ay), (bx, by) = P[o], P[d]
            sx, sy, ex, ey = ax + 100, ay + 50, bx, by + 50
            m = max(60, (ex - sx) / 2)
            partes.append(f'<path d="M{sx},{sy} C{sx + m},{sy} {ex - m},{ey} {ex},{ey}" stroke="#aaa" fill="none" stroke-width="2"/>')
    for padre, hijos in ai_hijos.items():
        for _, h in hijos:
            (ax, ay), (bx, by) = P[padre], P[h]
            partes.append(f'<path d="M{bx + 50},{by} C{bx + 50},{by - 50} {ax + 50},{ay + 150} {ax + 50},{ay + 100}" '
                          f'stroke="#888" fill="none" stroke-dasharray="6 4" stroke-width="2"/>')
    for n, (x, y) in P.items():
        t = nodos[n]["type"]
        if t == STICKY:
            p = nodos[n].get("parameters", {})
            partes.append(f'<rect x="{x}" y="{y}" width="{p.get("width", 240)}" height="{p.get("height", 160)}" fill="#fff3bf" opacity="0.8"/>')
            continue
        fill = "#495057" if n in es_sub else "#343a40"
        partes.append(f'<rect x="{x}" y="{y}" width="100" height="100" rx="10" fill="{fill}" stroke="#ced4da"/>')
        etiqueta = n if len(n) <= 26 else n[:25] + "…"
        partes.append(f'<text x="{x + 50}" y="{y + 122}" fill="#f8f9fa" font-size="13" text-anchor="middle">'
                      f'{etiqueta.replace("&", "&amp;").replace("<", "&lt;")}</text>')
    partes.append("</svg>")
    salida = str(Path(salida).resolve())
    html = Path(salida).with_suffix(".html")
    html.write_text(f'<html><body style="margin:0">{"".join(partes)}</body></html>')
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    subprocess.run([chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={int(W)},{int(H)}",
                    f"--screenshot={salida}", html.as_uri()], check=True, capture_output=True, timeout=120)
    return salida


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    wf = json.loads(Path(sys.argv[1]).read_text())
    print("Antes:", {k: (v if isinstance(v, int) else len(v)) for k, v in revisar(wf).items()})
    acomodar(wf)
    print("Después:", json.dumps(revisar(wf), ensure_ascii=False))
    if "--vista" in sys.argv:
        print("Vista previa:", vista_previa(wf, sys.argv[sys.argv.index("--vista") + 1]))
