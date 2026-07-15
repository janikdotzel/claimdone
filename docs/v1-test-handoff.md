# ClaimDone V1 test handoff

Stand: 15. Juli 2026. Dieses vorab versionierte Handoff gilt ausschließlich
für die lokale, deterministische V1 bis INT-002. Es ist weder Release- noch
Produktionsfreigabe.

## Abnahmestatus und Geltungsbereich

**Status: ausstehend.** Dieses Dokument und
[`evidence/int002-v1-acceptance.json`](evidence/int002-v1-acceptance.json)
definieren vor den gezählten Läufen den unveränderlichen Abnahmevertrag. Ihre
bloße Existenz ist kein PASS-Nachweis. Der Commit, der beide Dateien enthält,
wird erst dann zum Abnahme-Commit, wenn auf exakt diesem SHA:

1. zwei frische, aufeinanderfolgende Browserläufe bestanden sind;
2. zwischen den Läufen beide Dienste gestoppt, `make reset`, die
   Fixture-Regeneration samt `--check` und ein kompletter Dienstneustart
   nachgewiesen sind;
3. beide beobachteten Payloads nach dem versionierten Algorithmus denselben
   erwarteten SHA-256-Digest ergeben;
4. die fünf finalen Make-Kommandos bestanden sind; und
5. seit Beginn des ersten gezählten Laufs keine getrackte Datei verändert
   wurde.

Bis diese Evidenz vorliegt, darf weder das JSON-Feld `status` noch dieses
Handoff als `passed` berichtet werden. Die beiden Run-Slots besitzen getrennte
Ordnungsnummern. Ihre Case- und Event-Identitäten werden gehasht außerhalb des
semantischen Digests erfasst; Run 1 und Run 2 müssen unterschiedliche
Case- und erste Event-Hashes besitzen. Volatile IDs, Cursor und Zeitstempel
dürfen damit verschieden sein, die festgelegte Produktsemantik nicht.

Ziel der Abnahme ist ausschließlich dieser Pfad:

- drei deterministisch generierte PNGs in Manifest-Reihenfolge;
- der exakte Text aus `fixtures/int002/statement.txt`;
- für jedes Bild `Retain for this sandbox`;
- genau eine Rückfrage zum Feld `incident_time` mit Antwort `14:30:00`;
- der sichtbare Produktflow in einem lokalen Browser und Portal A über den
  getrennten lokalen semantischen Playwright-Adapter;
- G0 bis G8, ein beabsichtigter Mismatch, genau eine enge Reparatur und eine
  zweite erfolgreiche Verifikation;
- Endzustand `review`, ohne Agent-Approval und ohne Receipt.

Nicht Bestandteil der Abnahme sind Live-Provider, Audio, freie Eingaben, Portal B, Human
Approval, Receipt, W3, Release, Submission, Hosting oder Produktivbetrieb.

## Voraussetzungen

Vom Repository-Root aus werden exakt diese Runtimes erwartet:

- Node.js `24.14.0`
- pnpm `11.7.0`
- Python `3.12.13`
- uv `0.8.3`

Ein OpenAI-Key ist für diesen Test nicht erforderlich. Der V1-Pfad führt keine
externen Provideraufrufe aus. Falls eine spätere, ausdrücklich freigegebene
Live-Suite wegen Quota fehlschlägt, kann das am menschlich gesetzten
EUR-10-Plattformlimit liegen; das ist kein INT-002-Gate-Ergebnis.

## Setup und Start

```bash
make check-runtime
make setup
make reset
./.venv/bin/python scripts/generate_int002_fixtures.py
./.venv/bin/python scripts/generate_int002_fixtures.py --check
make dev
```

Erwartete Adressen:

- Web: <http://127.0.0.1:3000>
- API: <http://127.0.0.1:8000>
- Web-Health: <http://127.0.0.1:3000/health>
- API-Health: <http://127.0.0.1:8000/health>
- Produktflow: <http://127.0.0.1:3000/claim/new>

`make dev` bleibt während des Tests geöffnet. Mit `Ctrl-C` werden beide Dienste
beendet. `make reset` darf erst danach ausgeführt werden.

## Schrittweise Testanleitung

### 1. Disclosure

1. Öffne `/claim/new`.
2. Prüfe die Hinweise `Sandbox only`, `Human approval required` und
   `Deterministic INT-002 demo analysis`.
3. Aktiviere `I understand the sandbox and human-approval boundary`.
4. Wähle `Continue to intake`.

### 2. Intake

1. Wähle in dieser Reihenfolge:
   - `.local/int002-fixtures/01-synthetic-overview.png`
   - `.local/int002-fixtures/02-synthetic-rear-detail.png`
   - `.local/int002-fixtures/03-synthetic-context.png`
2. Prüfe bei allen drei Bildern `No embedded EXIF metadata detected.`.
3. Wähle bei jedem Bild `Retain for this sandbox`.
4. Lass `Written text` ausgewählt und kopiere den Inhalt von
   `fixtures/int002/statement.txt` ohne zusätzliche Zeichen in `What happened?`.
5. Aktiviere alle drei Einwilligungen.
6. Prüfe `Local G0 preflight passed` und `Local G1 preflight passed`.
7. Wähle `Analyze staged claim`.

### 3. Clarification

Erwartet werden:

- `One detail is still required`;
- `Wann ereignete sich der Vorfall?`;
- `Bound to server version 4 · field incident_time`.

Gib `14:30:00` ein und wähle `Answer and continue`. Der Portal-Lauf startet
automatisch. `Run Portal A` erscheint nur als begrenzter Retry, falls der
getrennte Run nicht abgeschlossen werden konnte.

### 4. Review

Erfolgreich ist der Lauf nur, wenn gleichzeitig sichtbar beziehungsweise im
autoritativen Snapshot belegt ist:

- `Verified Portal A review is ready`;
- Case-Zustand `review`, Version `9`;
- Portal A im Zustand `review`, Version `4`;
- der finale Snapshot G0 bis G8 in Reihenfolge und jeweils deterministisch
  bestanden zeigt;
- die unveränderliche Event-Historie zugleich die erste fehlgeschlagene
  G5-Entscheidung mit exakt `G5_REQUIRED_FIELD_MISSING` behält;
- `2 attempts · verified`;
- Versuch 1: beim Feld `incident_time` erwartet `14:30:00`, tatsächlich
  `00:00:00`, Review noch blockiert und genau eine enge Reparatur von
  Portal-Version 3 auf 4;
- Versuch 2: vollständiger Match und G8 bestanden;
- `agentCanSubmit=false`, keine Agent-Approval-Aktion, kein `human_approved`,
  `receipt=null`;
- genau ein redigiertes `provider_call`-Workflow-Ereignis mit
  `providerMode=mock`, aber null externe Provideraufrufe, Retries und operative
  Fehler;
- der Hinweis, dass der Agent weder approven noch submitten kann.

Der Link `Open Portal A review` muss `Layout A`, `Ready for human review`,
`2026-07-14 at 14:30:00`, `3 approved images` und den deaktivierten Button
`Human approval required in a separate context` zeigen.

## Gezählte Evidenzerfassung

Jeder Lauf zählt nur, wenn für seinen Run-Slot mindestens folgende Evidenz
erfasst wird:

- Ordinal 1 beziehungsweise 2;
- SHA-256 von Case-ID sowie erster und letzter persistierter Workflow-Event-ID;
- erster und letzter beobachteter SSE-Cursor;
- vollständige Reset-, Fixture- und Dienstneustart-Prüfung;
- der aus dem beobachteten semantischen Payload berechnete Digest;
- ein geöffneter SSE-Stream, während parallel sowohl `/health` als auch der
  autoritative Case-Snapshot HTTP 200 liefern; und
- der Abgleich dieses Snapshots mit dem finalen Case `review` in Version 9.

Die Identitätswerte beweisen zwei frische Läufe, werden aber nicht in den
semantischen Digest aufgenommen. Der Normalisierungsalgorithmus, die erlaubten
volatilen Unterschiede, der erwartete Payload und dessen erwarteter Digest
stehen im versionierten JSON. Ein beobachteter Wert darf nicht nachträglich an
den erwarteten Payload angepasst werden. Jede Änderung am Abnahmevertrag
erfordert einen neuen Commit und anschließend zwei neue gezählte Läufe.

## Technische Verifikation

Nach den zwei Browserläufen müssen auf demselben unveränderten Commit
ausgeführt werden:

```bash
make check-runtime
make lint
make typecheck
make test
make eval-deterministic
```

Die deterministische Evaluation muss zwölf bestandene Fälle, keine
fehlgeschlagenen Fälle und keine externen Provideraufrufe melden. Ein einzelnes
redigiertes Workflow-Ereignis `provider_call` im Modus `mock` ist Teil der
deterministischen Demoanalyse und darf nicht als externer Aufruf fehlgedeutet
werden. Solange ein Lauf-, Vergleichs- oder Kommando-Slot im JSON `pending`
ist, ist INT-002 nicht abgenommen.

## Troubleshooting

### `CLIENT_NETWORK_ERROR`

1. Prüfe, ob `make dev` noch läuft.
2. Öffne beide Health-Adressen. Beide müssen HTTP 200 liefern.
3. Prüfe, ob Ports 3000 oder 8000 bereits von einem alten Prozess belegt sind.
4. Stoppe den alten Prozess kontrolliert, beende `make dev`, führe `make reset`
   aus und starte neu.

### `Answer and continue` bleibt hängen

Prüfe zuerst API-Health und starte den Flow aus einem frischen Reset erneut.
Der in V1 integrierte Body-Limit-Middleware-Fix muss nach dem wiedergegebenen
Request-Body erneut an den echten ASGI-Receive-Kanal delegieren. Ein offener
SSE-Stream darf einen parallelen Health- oder Snapshot-Request nicht blockieren.

### Fixture wird abgelehnt

- Dateien neu generieren und `--check` ausführen.
- Upload-Reihenfolge kontrollieren.
- Für alle drei Bilder `retain` verwenden.
- Den Statement-Text nicht umformulieren und keinen zusätzlichen Zeilenumbruch
  einfügen.
- Exakt `14:30:00` antworten.

### Portal-Run schlägt fehl

Nicht in SQLite oder Portalwerten manuell korrigieren. Zuerst den angezeigten
begrenzten Retry verwenden. Bleibt der Fehler bestehen, Lauf als fehlgeschlagen
protokollieren, Dienste stoppen, `make reset` ausführen und reproduzieren.

## Bekannte Risiken und Grenzen

- V1 ist fixture-only; beliebige Nutzerbilder oder Texte sind nicht unterstützt.
- Der externe OpenAI-Key wird nicht verwendet; Live-Modellqualität ist nicht
  gemessen.
- Nur Portal A ist Teil der INT-002-Abnahme.
- Die Browser-Policy ist auf Loopback begrenzt, aber keine unabhängige
  OS-/Container-Egress-Sandbox.
- Der synchrone Browser-Bootstrap besitzt keinen garantierten harten Prozess-Kill.
- Human-Approval- und Receipt-Autorität sind negativ getestet, aber nicht Teil
  des V1-Hauptflows.
- Zwei erfolgreiche Läufe belegen Reproduzierbarkeit dieses Fixtures, nicht
  statistische Zuverlässigkeit für andere Inputs.
- Accessibility-, Responsive-, Performance-, Security- und Portal-B-Wellen sind
  bewusst W3 und nicht durch dieses Handoff freigegeben.

## Feedback-Backlog

Bitte neue Beobachtungen mit Priorität, Reproduktionsschritten, Screenshot und
erwartetem Verhalten ergänzen.

| ID | Priorität | Bereich | Offene Frage |
| --- | --- | --- | --- |
| V1-FB-001 | Offen | Disclosure | Ist in wenigen Sekunden klar, dass nichts eingereicht wird? |
| V1-FB-002 | Offen | Intake | Sind Upload, EXIF-Entscheidung und Einwilligungen verständlich? |
| V1-FB-003 | Offen | Clarification | Ist die eine Rückfrage präzise und der nächste Schritt erwartbar? |
| V1-FB-004 | Offen | Mismatch/Repair | Ist nachvollziehbar, warum der erste Versuch blockiert und eng repariert wurde? |
| V1-FB-005 | Offen | Review | Sind Provenienz, Gate Trail und Human Boundary ohne technisches Vorwissen verständlich? |
| V1-FB-006 | Offen | Fehler | Helfen die Fehlermeldungen beim selbstständigen Wiederanlauf? |

Ein P0-Feedback blockiert jede Folgefreigabe. Alle anderen Punkte bleiben im
Backlog, bis der Mensch nach dem Test ein neues Goal setzt.

## Handoff-Grenze

Auch nach einem später belegten PASS wird nicht mit Live-Providern, Portal B,
W3, Release, Submission oder Produktivsetzung begonnen. `main` und
`origin/main` bleiben unverändert, bis der Mensch Merge und Push ausdrücklich
freigibt.
