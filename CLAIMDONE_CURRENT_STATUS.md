# ClaimDone – aktueller Status und Produkttest

Stand: 15. Juli 2026. Diese Datei ist die kurze Produkt- und Testübersicht. Der ausführliche Backlog bleibt in [CLAIMDONE_IMPLEMENTATION_TASKS.md](CLAIMDONE_IMPLEMENTATION_TASKS.md).

## Kurzantwort

**Das lokale, deterministische V1 bis INT-002 ist als Acceptance Candidate
implementiert, aber noch nicht technisch abgenommen.** Der Branch
`codex/int002-v1-acceptance` enthält die vollständige Komposition, den
SSE-Middleware-Fix und den vorab versionierten Abnahmevertrag. Die zwei gezählten
Browserläufe auf einem finalen SHA und die anschließenden fünf Make-Gates stehen
noch aus.

Diese Aussage gilt nur für das exakte Fixture `claimdone-int002-main-v1`, Portal A
und den providerfreien Pfad bis `review`. Sie ist keine Freigabe für Live-AI,
beliebige Eingaben, Portal B, Approval, Receipt, Release oder Produktion.

`main` und `origin/main` enthalten die INT-002-Komposition, aber noch nicht den
finalen Middleware-/Handoff-Stand. Merge und Push erfolgen erst nach ausdrücklicher
Freigabe; bis dahin ist der Abnahme-Branch maßgeblich.

## Was ist erledigt?

| Bereich | Stand | Was das praktisch bedeutet |
| --- | --- | --- |
| Fundament, Verträge, G0–G5 und INT-001 | Erledigt | Lokale Projektbasis, gemeinsame Verträge, Medien-/Safety-/Provenienz-Gates und der frühere Walking Skeleton sind vorhanden. |
| Produkt-Frontend | Implementiert | Startseite, Disclosure, Intake, eine zeitgebundene Rückfrage, Workflow-/Evidence-Ansicht und Review-Darstellung sind als code-basiertes Designsystem vorhanden. |
| Sandbox-Portal | Implementiert | Lokales Portal mit Varianten A/B; nur Portal A gehört zum geplanten V1-Abnahmepfad. Es gibt keine echte Versicherungs- oder Submission-Funktion. |
| INT-002-Demoanalyse | Implementiert | Ein festes, synthetisches Fixture erzeugt deterministisch eine Rückfrage nach der Uhrzeit. Für diesen V1-Pfad sind externe Modellaufrufe deaktiviert. |
| G6–G8-Autorität | Implementiert und getestet | Portal-Run-Autorität, Feldschreibrechte, Mismatch-Erkennung, enge Reparatur und unabhängige Verifikation werden persistiert und fail-closed geprüft. |
| Vollständige Backend-Komposition | Erledigt | Die API verbindet Create, Intake, genau eine Rückfrage, Portal-Run, Mismatch, enge Reparatur und Review als `v1 → v4 → v5 → v9`. |
| Zwei E2E-Hauptläufe | Ausstehend | Die gezählten Läufe müssen nach dem SHA-Freeze mit vollständigem Reset durchgeführt und normalisiert verglichen werden. |
| V1-Test-Handoff | Acceptance Candidate | Testanleitung, erwartete normalisierte Evidenz, Risiken, Troubleshooting und Feedback-Backlog sind vorbereitet; die Ergebnis-Slots bleiben `pending`. |

Die automatisierten Kandidatenprüfungen für Runtime, Lint, Typecheck und Tests
sind vorläufig grün. Sie zählen erst erneut auf dem finalen unveränderten
Abnahme-SHA. `make eval-deterministic` verlangt dafür einen sauberen Arbeitsbaum.

## Was fehlt zur MVP-Abnahme?

1. Du führst den dokumentierten Produkttest aus und gibst Feedback zu
   Verständlichkeit, Vertrauen, Fehlerdarstellung und Human Boundary.
2. Codex friert danach den finalen SHA ein, führt zwei frische Browserläufe aus,
   vergleicht sie normalisiert und führt alle fünf Make-Gates erneut aus.
3. Beobachtungen werden als P0-Blocker oder späteres Feedback-Backlog klassifiziert.
4. Merge und Push nach `main` erfolgen nur nach deiner ausdrücklichen Freigabe.
5. Erst ein neues Goal darf W3 oder einen breiteren Produktumfang beginnen.

Die technische V1-Aussage bleibt bis zum vollständigen Abnahmenachweis ausstehend.
W3, Produktivsetzung und weitere Politur bleiben gesonderte Entscheidungen.

## Wie funktioniert das Produkt?

```text
Next.js-Frontend (Port 3000)
  Startseite → Disclosure → Intake → Rückfrage → Review-Oberfläche
  lokales Sandbox-Portal; Portal A ist der vorgesehene Abnahmepfad

FastAPI-Backend (Port 8000)
  Case-Versionen, SQLite-Persistenz, Gate-Entscheidungen,
  Demoanalyse und Portal-/Verifikations-Autorität

contracts/
  gemeinsame, geschlossene Datenverträge für Frontend und Backend
```

Das Frontend zeigt, was ein Nutzer eingibt, welche Rückfrage noch fehlt, welche Gates entschieden haben und warum der Prozess bei menschlichem Review endet. Das Backend ist die Autorität: Es verwaltet Zustände, Versionen, Gate-Ergebnisse und die lokale SQLite-Datenbank. Das Frontend darf diese Entscheidungen nicht selbst erzeugen oder überstimmen.

Alle Daten sind für die Demo synthetisch und lokal. ClaimDone kontaktiert weder einen Versicherer noch führt es eine Freigabe, Einreichung oder Zahlung aus.

## Was du jetzt als Produktmensch testen kannst

Der vollständige deterministische Happy Path ist implementiert, aber noch nicht
formal abgenommen. Für deinen Produkttest folge der Anleitung in
[`CLAIMDONE_MVP_TEST_AND_RESUME.md`](CLAIMDONE_MVP_TEST_AND_RESUME.md). Dein
Feedback ist jetzt besonders wertvoll für Verständlichkeit und Design:

1. Öffne die Startseite (`/`). Ist in wenigen Sekunden klar, was ClaimDone tut und wo die Automatisierung endet?
2. Öffne `/claim/new`. Bewerte Disclosure, Sprache, Einwilligungen, Bild-/Statement-Eingabe und die Fehlerhinweise. Fühlt sich der Ablauf ruhig, vertrauenswürdig und nicht überladen an?
3. Führe den Fixture-Flow bis `Verified Portal A review is ready` aus. Sind
   Rückfrage, Mismatch, Reparatur und zweiter Verifikationsversuch verständlich?
4. Öffne die verlinkte Portal-A-Review. Ist klar, dass „Review“ keine Einreichung
   bedeutet und der Agent nicht approven kann?
5. Öffne optional `/workflow-showcase` und `/components`, wenn du visuelles
   Detailfeedback zu Gate Trail, Statusfarben, Abständen und Lesbarkeit geben möchtest.

Für produktives Feedback helfen diese Fragen:

- Was dachtest du, passiert als Nächstes?
- Welche Information war zu technisch, zu lang oder hat gefehlt?
- An welcher Stelle würdest du zögern oder abbrechen?
- Was vermittelt Sicherheit – und was nicht?
- Welche visuelle Änderung würde den Flow spürbar verbessern?

Screenshots mit kurzen Kommentaren sind ideal. Ein technischer Fehler im exakten
Fixture-Flow ist P0; qualitative Verbesserungen kommen in das offene Feedback-Backlog.

## Verbindliche Stop-Grenze

Während dieser Pause beginnt keine weitere Implementierung. Insbesondere
Live-Provider, Portal B, W3, Release, Submission und Produktivsetzung warten auf
deinen Test, dein Feedback und das in
[`CLAIMDONE_MVP_TEST_AND_RESUME.md`](CLAIMDONE_MVP_TEST_AND_RESUME.md)
festgehaltene Fortsetzungs-Goal.
