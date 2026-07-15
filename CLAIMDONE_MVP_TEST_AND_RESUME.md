# ClaimDone MVP: menschlicher Test und Fortsetzungs-Goal

Stand: 15. Juli 2026.

Dieses Dokument hält den bewusst pausierten Zwischenstand fest. Der Branch
`codex/int002-v1-acceptance` enthält den implementierten deterministischen
INT-002-Pfad und den SSE-/Middleware-Fix. Die formale V1-Abnahme ist jedoch noch
nicht abgeschlossen: Die zwei gezählten Browserläufe auf einem finalen SHA und
die anschließenden fünf Make-Gates stehen weiterhin aus. `main` bleibt bis zu
einer ausdrücklichen Freigabe unverändert.

## Was du jetzt als Mensch prüfen kannst

Verwende ausschließlich das synthetische Fixture. Lade keine echten Fotos,
Versicherungsdaten oder andere personenbezogene Daten hoch.

### 1. Lokalen Kandidaten starten

Vom Repository-Root:

```bash
git switch codex/int002-v1-acceptance
git pull --ff-only
make check-runtime
make setup
make reset
./.venv/bin/python scripts/generate_int002_fixtures.py
./.venv/bin/python scripts/generate_int002_fixtures.py --check
make dev
```

Öffne anschließend:

- Produktflow: <http://127.0.0.1:3000/claim/new>
- Web-Health: <http://127.0.0.1:3000/health>
- API-Health: <http://127.0.0.1:8000/health>

Beide Health-Routen müssen erfolgreich antworten. `make dev` bleibt während
des Tests geöffnet und wird danach mit `Ctrl-C` beendet.

### 2. Exakten Fixture-Flow testen

1. Bestätige die Sandbox- und Human-Approval-Grenze.
2. Lade in einem Vorgang und in dieser Reihenfolge hoch:
   - `.local/int002-fixtures/01-synthetic-overview.png`
   - `.local/int002-fixtures/02-synthetic-rear-detail.png`
   - `.local/int002-fixtures/03-synthetic-context.png`
3. Wähle bei jedem Bild `Retain for this sandbox`.
4. Kopiere den Inhalt aus `fixtures/int002/statement.txt` in das Textfeld,
   aber ohne den einen abschließenden Zeilenumbruch der Datei.
5. Aktiviere alle drei Einwilligungen.
6. Warte auf die sichtbaren lokalen G0-/G1-Erfolgsmeldungen und wähle
   `Analyze staged claim`.
7. Es muss genau eine Rückfrage erscheinen:
   `Wann ereignete sich der Vorfall?`
8. Antworte exakt mit `14:30:00` und wähle einmal `Answer and continue`.
9. Während `Answering and running verified Portal A…` sichtbar ist, nicht
   erneut klicken.
10. Erwarteter Endzustand: `Verified Portal A review is ready`.
11. Öffne `Open Portal A review` und prüfe:
    - `Layout A`
    - Status `Ready for human review`
    - Datum und Uhrzeit `2026-07-14 at 14:30:00`
    - drei freigegebene Bilder
    - deaktivierte Human-Approval-Aktion
    - kein Hinweis auf Einreichung oder Receipt

Falls der Flow technisch fehlschlägt, notiere bitte:

- den sichtbaren Fehlercode und Wortlaut;
- den Schritt, an dem der Fehler auftrat;
- ob beide Health-Routen noch erreichbar waren;
- einen Screenshot ohne persönliche Daten;
- ob ein erneuter Lauf nach `Ctrl-C`, `make reset` und Neustart denselben
  Fehler reproduziert.

### 3. Produkt- und UX-Feedback geben

Bitte bewerte insbesondere:

- Ist in wenigen Sekunden klar, dass ClaimDone nichts einreicht?
- Sind Upload, EXIF-Entscheidung und Einwilligungen verständlich?
- Ist die eine Rückfrage präzise und der nächste Schritt erwartbar?
- Ist nachvollziehbar, warum der erste Portalwert blockiert und eng repariert
  wurde?
- Sind Gate Trail, Provenienz und Human Boundary ohne technisches Vorwissen
  verständlich?
- Helfen Fehlertexte beim selbstständigen Neustart?
- Fühlt sich die Oberfläche ruhig, vertrauenswürdig und ausreichend klar an?

Feedback bitte möglichst so erfassen:

```text
Priorität: P0 | P1 | P2
Bereich: Disclosure | Intake | Clarification | Review | Fehler | Design
Beobachtung:
Erwartetes Verhalten:
Reproduktionsschritte:
Screenshot vorhanden: ja | nein
```

Ein technischer Fehler im exakten Fixture-Flow ist P0 und blockiert die
Abnahme. Qualitative Wünsche kommen zunächst in den Feedback-Backlog.

### 4. Entscheidung nach deinem Test

Bitte entscheide anschließend ausdrücklich zwischen:

1. P0-Fehler beheben und die INT-002-Abnahme wiederholen;
2. das unten stehende Fortsetzungs-Goal ausführen;
3. nach vollständig bestandenem Handoff Merge und Push nach `main` separat
   freigeben.

Bis dahin beginnen keine Live-Provider-, Portal-B-, W3-, Release-, Submission-
oder Produktionsarbeiten.

## Noch offene technische Abnahme

Beim Fortsetzen muss Codex auf einem einzigen finalen SHA:

1. Handoff-, Status- und Evidenzdokumente wahrheitsgemäß finalisieren;
2. zwei frische, aufeinanderfolgende sichtbare Browserläufe durchführen;
3. zwischen den Läufen Dienste stoppen, `make reset` ausführen, das Fixture neu
   generieren und prüfen sowie beide Dienste neu starten;
4. pro Lauf die Folge `v1 → v4 → v5 → v9`, genau eine Clarification, G0–G8,
   den beabsichtigten `incident_time`-Mismatch, eine enge Reparatur, die zweite
   erfolgreiche Verifikation und `review` ohne Agent-Approval und Receipt
   belegen;
5. beide Läufe identitätsunabhängig normalisieren und exakt vergleichen;
6. anschließend auf demselben sauberen SHA ausführen:

```bash
make check-runtime
make lint
make typecheck
make test
make eval-deterministic
```

Jede getrackte Änderung nach dem SHA-Freeze erzwingt einen neuen SHA und die
vollständige Wiederholung beider Browserläufe und aller Gates.

## Empfohlenes Fortsetzungs-Goal für Codex

> Schließe ausschließlich die deterministische ClaimDone V1 bis zum bestandenen
> INT-002 ab. Setze auf dem gepushten Branch `codex/int002-v1-acceptance` fort
> und prüfe zuerst den aktuellen HEAD sowie alle user-owned Änderungen. Bringe
> die versionierte Abnahme-, Evidenz- und Handoff-Dokumentation auf einen
> wahrheitsgemäßen finalen Stand, ohne `buildweek-diary.md` oder andere
> user-owned Inhalte zu überschreiben. Erzeuge einen finalen Abnahme-SHA. Führe
> auf exakt diesem SHA zwei frische, aufeinanderfolgende sichtbare Browser-E2E-
> Läufe mit `claimdone-int002-main-v1` durch, jeweils nach vollständigem Reset,
> Fixture-Regeneration, Fixture-Prüfung und Dienstneustart. Belege je Lauf genau
> eine `incident_time`-Clarification mit `14:30:00`, die Versionsfolge
> `v1 → v4 → v5 → v9`, den finalen grünen G0–G8-Snapshot, den historischen
> beabsichtigten G5-Fehler, den einzigen `incident_time`-Mismatch, die enge
> Reparatur von Portal-v3 auf v4, die zweite erfolgreiche G8-Verifikation,
> Portal A und Case im Zustand `review`, `agentCanSubmit=false`, keine Human
> Approval und `receipt=null`. Prüfe außerdem genau ein Mock-Provider-Ereignis,
> null Live-Provider-/Retry-/Operational-Failure-Ereignisse und dass ein offener
> SSE-Stream parallele Health- und Snapshot-Requests nicht blockiert. Vergleiche
> beide Läufe nach dokumentierter Normalisierung exakt. Führe danach auf
> demselben sauberen SHA `make check-runtime`, `make lint`, `make typecheck`,
> `make test` und `make eval-deterministic` aus. Bei jeder Änderung des SHA
> beginne beide E2E-Läufe und alle Gates erneut. Liefere anschließend den
> vollständigen V1-Test-Handoff mit Ergebnissen, Risiken, Troubleshooting,
> Testanleitung und Feedback-Backlog und stoppe. Beginne keine Live-Provider-,
> Portal-B-, W3-, Release-, Submission- oder Produktivsetzungsarbeit. Merge und
> Push nach `main` erfolgen nur nach meiner ausdrücklichen Freigabe.
