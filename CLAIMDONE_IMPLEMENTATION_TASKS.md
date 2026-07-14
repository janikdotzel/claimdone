# ClaimDone Build Week: ausführbarer Aufgaben- und Worktree-Plan

Dieses Dokument übersetzt den freigegebenen [`CLAIMDONE_BUILD_WEEK_PLAN.md`](CLAIMDONE_BUILD_WEEK_PLAN.md) in einzeln beauftragbare Aufgaben. Es ist die operative Grundlage für Codex-Agenten, menschliche Entscheidungen, parallele Git-Worktrees, Integration und Abnahme.

## 1. Arbeitsmodell

### Rollen

- **Codex:** Implementierung, automatisierte Tests, technische Dokumentation, statische Analyse und reproduzierbare Verifikation.
- **Mensch:** Produktentscheidungen mit Außenwirkung, Zugangsdaten, Rechte an Demo-Material, qualitative Produkttests, Videoaufzeichnung, Veröffentlichung und Devpost-Einreichung.
- **Hybrid:** Codex bereitet Artefakte und Optionen vor; ein Mensch prüft Wirkung, Sicherheit oder rechtliche Außenwirkung und gibt die nächste Stufe frei.

### Worktree-Grundsätze

Codex-Worktrees eignen sich für unabhängige parallele Aufgaben und können später per Handoff oder Branch/PR in den lokalen Integrationsstand überführt werden. Managed Worktrees starten standardmäßig mit einem detached `HEAD`; vor Commits muss im Worktree ein Branch angelegt werden. Wenn nur eine laufende App-Instanz sinnvoll ist, soll die finale Integration im lokalen Checkout stattfinden. [Offizielle Codex-Worktree-Dokumentation](https://developers.openai.com/codex/app/worktrees)

Subagents sind vor allem für unabhängige, read-heavy Aufgaben wie Recherche, Reviews, Log-Analyse oder Test-Triage geeignet. Gleichzeitige write-heavy Subagents im selben Worktree sollen vermieden werden, weil sie Konflikte und Koordinationsaufwand erhöhen. [Offizielle Codex-Subagent-Dokumentation](https://developers.openai.com/codex/subagents)

Verbindliche Regeln:

1. Vor dem ersten Worktree muss `main` einen sauberen, committed Planungsstand besitzen.
2. Jeder Worktree erhält einen Branch mit Präfix `codex/`.
3. Jeder Worktree besitzt klar definierte Verzeichnisse. Fremde Verzeichnisse werden nur nach ausdrücklicher Integrationsabsprache verändert.
4. Root-Manifeste und Lockfiles haben pro Welle genau einen Eigentümer.
5. Jeder Task endet mit Commit, Testnachweis, Änderungszusammenfassung und bekannten Risiken.
6. Kein Agent merged selbstständig nach `main`; Integration erfolgt kontrolliert im lokalen Checkout.
7. Nach jeder Integrationswelle werden alle nachfolgenden Worktrees vom aktualisierten `main` neu erstellt oder rebased.
8. Maximal vier write-heavy Worktrees gleichzeitig. Dokumentation, Reviews und menschliche Aufgaben dürfen zusätzlich laufen.
9. Subagents innerhalb eines Worktrees dürfen parallel analysieren oder reviewen, aber nicht gleichzeitig dieselben Dateien bearbeiten.
10. Reale Secrets werden nicht committed. `.worktreeinclude` darf nur bewusst ausgewählte ignorierte lokale Konfiguration kopieren; echte API-Keys werden vorzugsweise pro Umgebung injiziert.

### Geplante Worktrees

| Worktree | Branch | Eigentum | Laufzeit |
| --- | --- | --- | --- |
| Foundation | `codex/foundation` | Root-Konfiguration, Toolchain, CI, Commands | Welle 0 |
| Contracts & Gates | `codex/contracts-gates` | Kanonische Typen, Zustände, G0–G5 | Wellen 0–1 |
| Frontend Experience | `codex/frontend-experience` | ClaimDone-Produktoberfläche | Wellen 1–3 |
| Sandbox Portal | `codex/sandbox-portal` | Portal-UI, Portalzustände, Human Approval | Wellen 1–2 |
| Backend Workflow | `codex/backend-workflow` | Case API, Persistenz, Audit, Events | Wellen 1–2 |
| AI Workflow | `codex/ai-workflow` | Transkription, GPT-5.6, Planner, Narrative | Welle 2 |
| Computer Use | `codex/computer-use` | Browser-Runner, G6–G8, Verifier | Welle 2 |
| Evals & Quality | `codex/evals-quality` | Dataset, Grader, E2E, Security, Reports | Wellen 1–3 |
| Docs & Submission | `codex/docs-submission` | README, Architektur, Demo- und Testdokumente | Wellen 1–4 |

## 2. Kritischer Startzustand

Der lokale Checkout ist aktuell nicht sauber: Es gibt bestehende Änderungen und Löschungen sowie den neuen untracked Build-Week-Plan. Worktrees dürfen nicht blind vom bisherigen `main` gestartet werden, weil sonst Plan und Spezifikationsstand auseinanderlaufen.

### HUM-000 – Planungsstand freigeben und Git-Baseline herstellen

- **Owner:** Mensch
- **Ausführung:** Lokaler Checkout, kein Worktree
- **Abhängigkeiten:** keine
- **Blockiert:** alle Codex-Implementierungstasks
- **Aufgabe:** Prüfen, ob die Löschungen von `ACCIDENT_TO_CLAIM_AGENT_SPEC.md` und `BUILD_WEEK_DIARY.md` sowie die Änderungen an `BUILD_WEEK_SPEC.md` beabsichtigt sind. Den neuen Build-Week-Plan und diesen Aufgabenplan freigeben. Danach einen bewussten Baseline-Commit herstellen.
- **Akzeptanz:** `git status` ist sauber; `main` enthält die freigegebenen Planungsdokumente; versehentliche Nutzeränderungen wurden weder verworfen noch überschrieben.

### HUM-001 – OpenAI-Zugang und Kostenrahmen bereitstellen

- **Owner:** Mensch
- **Ausführung:** Außerhalb des Repositories
- **Abhängigkeiten:** keine
- **Parallel:** HUM-000, Design- und Fixture-Arbeit
- **Aufgabe:** Einen nutzbaren `OPENAI_API_KEY`, Zugriff auf GPT-5.6, Computer Use und Audio-Transkription sicherstellen. Persönliches Kostenlimit festlegen und prüfen, ob Build-Week-Credits beantragt beziehungsweise verfügbar sind.
- **Akzeptanz:** Der Key ist nur als Environment-Secret verfügbar; kein Secret steht in Chat, Git, Screenshots oder Demo-Logs; Kostenlimit und verantwortliche Person sind dokumentiert.

### HUM-002 – Demo-Material und Nutzungsrechte festlegen

- **Owner:** Mensch
- **Ausführung:** Außerhalb des Codes; Artefakte später in `fixtures/`
- **Abhängigkeiten:** keine
- **Parallel:** Welle 0 und Design
- **Aufgabe:** Zwei inszenierte, nicht sensible Dreier-Bildsets auswählen oder erstellen. Echte Personen, Kennzeichen, Adressen und Versicherungsdaten dürfen nicht sichtbar sein. Zusätzlich ein deutsches, ein englisches Statement und ein kurzes Audiofixture bereitstellen.
- **Akzeptanz:** Rechte zur Nutzung und Veröffentlichung sind geklärt; Dateien enthalten keine realen sensiblen Daten; ein Hauptfixture erzeugt fachlich genau eine sinnvolle Rückfrage.

### UX-001 – Figma-Kernflow und visuelle Richtung freigeben

- **Owner:** Hybrid; Codex erstellt, Mensch entscheidet
- **Ausführung:** Figma, kein Code-Worktree
- **Abhängigkeiten:** freigegebener Build-Week-Plan
- **Parallel:** HUM-000 bis HUM-002 und Welle 0
- **Ziel:** Vor der Frontend-Implementierung Interaktionsfluss, Informationshierarchie und zentrale Zustände sichtbar entscheiden.
- **Aufgabe:** Einen kompakten klickbaren Flow für Disclosure, Intake, EXIF-Entscheidung, Evidence Board, Clarification, Agent Run, Mismatch, Review, Human Approval und Receipt erstellen. Designrichtung Navy/Teal, Statusfarben, Desktop-Viewport und Kernkomponenten dokumentieren. Bei Ausführung durch Codex sind die passenden Figma-Skills vor Figma-Schreibaktionen zu laden.
- **Akzeptanz:** Alle P0-Zustände sind abgebildet; Sandbox- und Human-Boundary-Hinweise sind auf Intake und Review sichtbar; Mensch gibt Flow und visuelle Richtung frei; keine vollständige High-Fidelity-Ausarbeitung aller Responsive-Varianten erforderlich.

### HUM-008 – Repository-Sichtbarkeit und Lizenz festlegen

- **Owner:** Mensch, Vorbereitung durch Codex möglich
- **Ausführung:** Repository-/GitHub-Entscheidung
- **Abhängigkeiten:** keine
- **Parallel:** Wellen 0–3
- **Aufgabe:** Entscheiden, ob das Repository öffentlich mit geeigneter Open-Source-Lizenz oder privat mit Devpost-/OpenAI-Judging-Zugriff eingereicht wird. Bei öffentlichem Repository konkrete Lizenz freigeben; Codex darf die zugehörige `LICENSE`-Datei danach exakt übernehmen, aber keine rechtliche Entscheidung erfinden.
- **Akzeptanz:** Sichtbarkeit und Lizenz-/Sharing-Weg sind dokumentiert; Release Gate kann die Entscheidung deterministisch prüfen; notwendige Judge-Adressen werden erst beim finalen Live-Abgleich bestätigt.

## 3. Welle 0 – Fundament, Verträge und Arbeitsumgebung

Diese Welle ist weitgehend sequenziell. Erst nach ihrem Merge werden die großen parallelen Worktrees gestartet.

### FND-001 – Monorepo und minimale Laufzeit aufsetzen

- **Owner:** Codex
- **Worktree:** Foundation
- **Abhängigkeiten:** HUM-000
- **Datei-Eigentum:** Root-Konfiguration, `apps/`, `services/` nur als Scaffold
- **Ziel:** Einen startbaren, aber fachlich leeren Monorepo-Rahmen schaffen.
- **Umsetzung:**
  - Zuerst die verfügbare Workspace-Runtime prüfen, weil `node` im aktuellen Shell-Pfad noch nicht verfügbar ist; anschließend Node-/pnpm- und Python-/uv-Versionen reproduzierbar festlegen.
  - `pnpm`-Workspace mit Next.js/TypeScript-App anlegen.
  - Python-Projekt mit `uv`, FastAPI und pytest anlegen.
  - Verzeichnisstruktur für `apps/web`, `services/api`, `contracts`, `fixtures`, `evals`, `docs` und `scripts` erzeugen.
  - TypeScript strict mode, ESLint und Python-Linting/Typing konfigurieren.
  - `/health` in Web und API implementieren.
  - Keine fachlichen Claim-Features implementieren.
- **Deliverables:** Root-Manifeste, App-Scaffolds, Healthchecks, initiale Tests.
- **Akzeptanz:** Frischer Checkout kann Abhängigkeiten installieren; Web und API starten; Healthchecks liefern Erfolg; Lint und Basistests laufen.
- **Handoff:** Exakte Setup- und Testkommandos, geänderte Dateien, benötigte Systempakete und offene Installationsprobleme nennen.

### CON-001 – Kanonische Verträge und Zustandsmaschinen definieren

- **Owner:** Codex
- **Worktree:** Contracts & Gates
- **Abhängigkeiten:** FND-001 muss gemerged sein
- **Parallel:** FND-002 nach abgestimmten Root-Manifests
- **Datei-Eigentum:** `contracts/`, Pydantic-Vertragsmodule, generierte TypeScript-Typen
- **Ziel:** Eine stabile Schnittstelle schaffen, auf der Frontend, Backend, Portal, Computer Use und Evals unabhängig aufbauen können.
- **Umsetzung:**
  - `ClaimPacket`, `ClaimScope`, `EvidenceItem`, `EvidenceFact`, `ProvenanceRef`, `ClaimData`, `ToolPlan`, `PlanStep`, `GateDecision`, `VerificationReport`, `AuditEvent`, `EvalCase` und `ReleaseDecision` definieren.
  - Enums für Faktenstatus, CaseState, PortalState, VerificationState und GateReasonCode festlegen.
  - Workflow `created → disclosed → analyzing → awaiting_clarification → ready_to_fill → filling → verifying → review|blocked → human_approved → receipt` validieren.
  - Zusätzliche Endzustände `emergency_stopped`, `abandoned`, `failed` abbilden.
  - JSON Schema/OpenAPI als Quelle für generierte TypeScript-Typen einrichten.
  - Beispielobjekte für Happy Path, Block und Mismatch bereitstellen.
- **Akzeptanz:** Ungültige Zustandsübergänge und unbekannte Felder werden abgelehnt; Python- und TypeScript-Verträge stimmen überein; Beispiele validieren automatisiert.
- **Handoff:** Version der Verträge und Breaking-Change-Regel dokumentieren. Nach Merge dürfen andere Worktrees die Verträge nicht lokal neu erfinden.

### FND-002 – Reproduzierbare Commands, CI und Worktree-Setup erstellen

- **Owner:** Codex
- **Worktree:** Foundation
- **Abhängigkeiten:** FND-001
- **Parallel:** CON-001
- **Datei-Eigentum:** Root-Commands, CI, Environment-Dokumentation
- **Ziel:** Jeder Codex-Worktree soll ohne manuelle Sonderwege installierbar und prüfbar sein.
- **Umsetzung:**
  - `make setup`, `make dev`, `make test`, `make lint`, `make typecheck`, `make reset` definieren.
  - `.env.example` mit ausschließlich Platzhaltern anlegen.
  - Optional `.worktreeinclude` nur für ausdrücklich freigegebene, ignorierte und nicht geheime lokale Dateien konfigurieren.
  - GitHub Actions für Lint, Typecheck und Basistests einrichten.
  - Setup-Skript idempotent gestalten.
  - `AGENTS.md` um Commands, Verzeichnis-Eigentum, Gate-Vorrang und Verifikationspflicht ergänzen.
- **Akzeptanz:** Zweiter Setup-Lauf verändert nichts Unerwartetes; CI verwendet dieselben Commands wie lokal; keine Secrets werden kopiert oder geloggt.

### INT-000 – Fundament integrieren und Parallelbasis markieren

- **Owner:** Mensch mit Codex-Unterstützung
- **Ausführung:** Lokaler `main`
- **Abhängigkeiten:** FND-001, CON-001, FND-002
- **Ziel:** Einen einzigen stabilen Startcommit für alle parallelen Worktrees herstellen.
- **Umsetzung:** Branches einzeln prüfen und mergen; Lockfiles neu erzeugen; alle Basischecks ausführen; Tag oder Commit-ID als `parallel-base` dokumentieren.
- **Akzeptanz:** `main` ist sauber; Setup, Lint, Typecheck und Tests sind grün; alle Welle-1-Worktrees werden exakt von diesem Commit gestartet.

## 4. Welle 1 – Parallel laufendes Walking Skeleton

Nach INT-000 können Backend, Frontend, Portal, Gates, Evals und Dokumentation parallel laufen.

### BE-001 – Case API, SQLite-Persistenz und Audit-Grundlage

- **Owner:** Codex
- **Worktree:** Backend Workflow
- **Abhängigkeiten:** INT-000
- **Parallel:** FE-001, PORT-001, GATE-001, EVAL-001, DOC-001
- **Datei-Eigentum:** `services/api/app/cases`, `persistence`, `audit`
- **Ziel:** Cases sicher erzeugen, lesen, aktualisieren und löschen können.
- **Umsetzung:**
  - SQLite-Schema und Migration für CaseState, PortalState, redigierte Claim-Metadaten und AuditEvents implementieren.
  - `POST /api/cases`, `GET /api/cases/{id}` und `DELETE /api/cases/{id}` implementieren.
  - Zustandsübergänge ausschließlich über validierte Servicefunktionen erlauben.
  - Idempotenten Demo-Reset vorbereiten.
  - Keine OpenAI-Aufrufe integrieren.
- **Akzeptanz:** API- und Persistenztests decken Create/Get/Delete, ungültige Übergänge, parallele Updates und Löschung ab; AuditEvents enthalten keine Rohdaten.

### MEDIA-001 – Sichere Intake- und Medienpipeline mit G0/G1

- **Owner:** Codex
- **Worktree:** Contracts & Gates
- **Abhängigkeiten:** INT-000, HUM-002 für echte Fixtures; bis dahin synthetische Platzhalter
- **Parallel:** BE-001, FE-001
- **Datei-Eigentum:** Gate-Module G0/G1, lokale Mediennormalisierung
- **Ziel:** Drei Bilder oder Audio deterministisch prüfen, normalisieren und datenschutzgerecht speichern.
- **Umsetzung:**
  - Exakt drei JPG/PNG über MIME und Magic Bytes prüfen; 10-MB-Limit erzwingen.
  - Text XOR Audio und 60-Sekunden-Audiolimit prüfen.
  - Einwilligungen validieren.
  - EXIF lokal lesen, anzeigenfähige Zusammenfassung erzeugen und bereinigte Modellkopie erstellen.
  - Temporäre Case-Verzeichnisse mit sicherer Dateibenennung verwenden.
  - Reset/Delete entfernt Bilder, Audio, Transkripte und temporäre Kopien.
- **Akzeptanz:** Falsche Anzahl, Typ, Magic Bytes, Größe, fehlende Einwilligung und überlanges Audio werden vor jedem Modellaufruf blockiert; Löschtests bestätigen vollständige Entfernung.

### GATE-001 – Gate Registry und G2–G5 implementieren

- **Owner:** Codex
- **Worktree:** Contracts & Gates
- **Abhängigkeiten:** CON-001, MEDIA-001 innerhalb desselben Worktrees
- **Parallel:** BE-001, FE-001, PORT-001
- **Ziel:** Output Contract, Safety, Provenienz und Vollständigkeit als deterministische, unveränderliche Entscheidungen umsetzen.
- **Umsetzung:**
  - Zentrale Gate Registry mit Reihenfolge und `GateDecision`-Events implementieren.
  - G2 validiert Strict Schema, Quellenreferenzen, Refusal und maximal einen Retry.
  - G3 blockiert deterministische Safety- und Scope-Regeln; Modellklassifikation darf nur zusätzlich blockieren.
  - G4 erzwingt Provenienz, verbotene Bildinferenz und Confidence-Schwelle 0,80.
  - G5 berechnet fehlende Pflichtfelder, stellt jeweils nur eine Frage und blockiert nach drei Runden.
  - Kein Gate darf von einem Modell oder UI-Flag überschrieben werden.
- **Akzeptanz:** Parametrisierte Tests decken jeden ReasonCode, Priorität mehrerer Blocks und Unveränderlichkeit der Entscheidungen ab.

### FE-001 – Designsystem und Produktoberflächen-Shell

- **Owner:** Codex, visuelle Freigabe durch Mensch
- **Worktree:** Frontend Experience
- **Abhängigkeiten:** INT-000
- **Parallel:** BE-001, PORT-001, GATE-001
- **Datei-Eigentum:** Produktbereiche von `apps/web`
- **Ziel:** Einen konsistenten, zugänglichen Rahmen für alle ClaimDone-Zustände schaffen.
- **Umsetzung:**
  - Tokens für Farben, Typografie, Abstände, Radien und Statussemantik definieren.
  - Sandbox-Banner, Stepper, Cards, Buttons, Inputs, Alerts, Provenance Chip und Gate Badge bauen.
  - Responsive Desktop-first Shell mit Haupt- und Seitenpanel erstellen.
  - Fokuszustände, Tastaturbedienung und Kontrast berücksichtigen.
  - Story-/Showcase-Routen für Komponenten bereitstellen, falls kein Storybook genutzt wird.
- **Akzeptanz:** Komponenten besitzen Empty, Loading, Error, Blocked und Success States; automatisierte Accessibility-Basischecks bestehen; keine reale Versicherungsmarke erscheint.

### FE-002 – Disclosure- und Intake-Flow

- **Owner:** Codex
- **Worktree:** Frontend Experience
- **Abhängigkeiten:** FE-001, Contracts aus INT-000
- **Parallel:** MEDIA-001
- **Ziel:** Nutzer können den Sandbox-Hinweis verstehen und einen gültigen Intake vorbereiten.
- **Umsetzung:**
  - Exakt drei Bilder per Auswahl/Drag-and-drop, Preview und Entfernen unterstützen.
  - Text- oder Audioeingabe mit 60-Sekunden-Grenze anbieten.
  - Einwilligungen für Sandbox, Bildrechte und Datenverarbeitung erzwingen.
  - EXIF-Ergebnisse und Auswahl Entfernen/Beibehalten anzeigen.
  - Alle Backend-Validierungsfehler feldnah darstellen.
- **Akzeptanz:** Weiter-Button bleibt bis zu gültigem G0/G1 deaktiviert; Tastatur- und Fehlerflows funktionieren; deutsche und englische Texteingaben werden nicht clientseitig verfälscht.

### PORT-001 – Sandbox-Portal, Zustände und Layoutvarianten

- **Owner:** Codex
- **Worktree:** Sandbox Portal
- **Abhängigkeiten:** INT-000
- **Parallel:** Frontend, Backend, Gates
- **Datei-Eigentum:** Sandbox-Routen und Portalmodule von `apps/web`; keine Produkt-Shell-Dateien
- **Ziel:** Ein echtes, resetbares Multi-Step-Formular mit zwei semantisch gleichen DOM-Varianten schaffen.
- **Umsetzung:**
  - Portalzustände `draft`, `review`, `human_approved`, `receipt` abbilden.
  - Felder für Datum, Zeit, Ort, Claimant, Policy, Vehicle, Counterparty, Narrative und drei Anhänge implementieren.
  - Layout A als Standardformular und Layout B mit anderer Reihenfolge sowie mindestens einer abweichenden Label-Control-Beziehung bauen.
  - Werte serverseitig auditieren und Read-only-Ansicht vorbereiten.
  - Developer Reset und Fixture-Auswahl anbieten.
- **Akzeptanz:** Beide Varianten führen manuell mit denselben Daten zu identischem Reviewzustand; Reload erhält Zustand; ungültige Statussprünge scheitern serverseitig.

### EVAL-001 – Eval-Schema und erste zwölf Ground-Truth-Fälle

- **Owner:** Codex, Fixture-Inhalte durch Mensch prüfbar
- **Worktree:** Evals & Quality
- **Abhängigkeiten:** INT-000, HUM-002 teilweise
- **Parallel:** alle Welle-1-Tasks
- **Datei-Eigentum:** `evals/`, Test-Fixtures ohne Medienpipeline-Code
- **Ziel:** Schon vor vollständiger KI-Integration ein maschinenlesbares Qualitätsziel schaffen.
- **Umsetzung:**
  - EvalCase-Format mit Input, erlaubten/verbotenen Fakten, erwarteten Missing Fields, Frage, Tools, Gates, Portalwerten und Endzustand implementieren.
  - Zwölf Fälle erstellen: Happy Paths DE/EN, Missing Fields, Unsicherheit, Safety und Injection.
  - Dataset-Validator und eindeutige IDs bereitstellen.
  - Keine echten personenbezogenen Daten verwenden.
- **Akzeptanz:** Alle Fälle validieren gegen den Vertrag; jeder Safety-Fall besitzt expliziten erwarteten GateReasonCode; Dataset kann ohne Live-Modell geladen werden.

### DOC-001 – README- und Architekturdokument-Skelett

- **Owner:** Codex
- **Worktree:** Docs & Submission
- **Abhängigkeiten:** INT-000
- **Parallel:** gesamte Implementierung
- **Ziel:** Dokumentation nicht bis zum letzten Tag aufschieben.
- **Umsetzung:** README-Struktur für Problem, Scope, Architektur, Setup, Run, Reset, Sample Flow, Codex-Nutzung, GPT-5.6-Nutzung und Limitations erstellen. Architekturdiagramm und Platzhalter für verifizierte Ergebnisse anlegen. Nicht implementierte Funktionen ausdrücklich als geplant markieren.
- **Akzeptanz:** Keine unbewiesenen Produktclaims; Setup-Platzhalter sind klar gekennzeichnet; Dokumentation verweist auf Contracts, Gates und Eval-Ziele.

### INT-001 – Walking-Skeleton-Integration

- **Owner:** Mensch mit Codex-Unterstützung
- **Ausführung:** Lokaler `main`
- **Abhängigkeiten:** BE-001, MEDIA-001, GATE-001, FE-001, FE-002, PORT-001
- **Ziel:** Erster vollständiger Flow ohne Live-KI: Intake → gemocktes ClaimPacket → eine Clarification → Portal A → Review.
- **Umsetzung:** Worktree-Branches in genannter Reihenfolge integrieren; Contracts nicht während des Merges ad hoc ändern; Mock-Adapter verwenden; Root-Lockfiles einmal final regenerieren.
- **Akzeptanz:** Flow funktioniert nach `make setup && make dev`; Reset ist reproduzierbar; Test-, Lint- und Typecheck-Suite grün; keine manuelle Datenbankbearbeitung nötig.

## 5. Welle 2 – KI, Computer Use, Verifier und Human Boundary

Diese Welle startet erst nach INT-001. AI, Computer Use, Frontend-Ausbau, Portal-Sicherheit und Eval-Grader können parallel laufen.

### AI-001 – Audio-Transkription integrieren

- **Owner:** Codex
- **Worktree:** AI Workflow
- **Abhängigkeiten:** INT-001, HUM-001
- **Parallel:** AI-002 nach gemeinsamem Adapter-Interface, CU-001, FE-003
- **Ziel:** Kurze deutsche und englische Sprachmemos sicher in Text überführen.
- **Umsetzung:** OpenAI-Audioadapter für `gpt-4o-transcribe`, Timeout, Dateigrößenprüfung, Fehlerzustände und redigierte Metriken implementieren. GPT-5.6 erhält ausschließlich das bestätigte Transkript, nie Roh-Audio.
- **Akzeptanz:** Audiofixture wird korrekt transkribiert; Fehlversuche erzeugen keinen Folgeaufruf; Audio wird nach Reset gelöscht; Logs enthalten keinen vollständigen Inhalt.

### AI-002 – GPT-5.6 Evidence Extraction mit Strict Structured Output

- **Owner:** Codex
- **Worktree:** AI Workflow
- **Abhängigkeiten:** INT-001, HUM-001, CON-001
- **Parallel:** CU-001, FE-003, AUTH-001
- **Ziel:** Drei Bilder und Aussage/Transkript in ein schema-valides, evidenzverknüpftes ClaimPacket überführen.
- **Umsetzung:**
  - Responses-API-Adapter mit GPT-5.6 und drei Bildinputs implementieren.
  - Strict Structured Output direkt aus kanonischem Schema nutzen.
  - Prompt trennt `observed`, `user_stated`, `unknown`, `not_supported`.
  - Sensible Identität, Policy, Adresse, Kennzeichen, VIN, Schuld und Kosten dürfen nicht aus Bildern inferiert werden.
  - Refusal, Timeout, abgeschnittene Ausgabe und ein begrenzter Retry behandeln.
- **Akzeptanz:** Kernfixtures liefern schema-valide Pakete; verbotene Felder bleiben unbekannt; jede beobachtete Tatsache besitzt existierende Quellen; G2/G4 entscheiden erfolgreich oder blockieren nachvollziehbar.

### AI-003 – Narrative Composer, Planner und Toolauswahl

- **Owner:** Codex
- **Worktree:** AI Workflow
- **Abhängigkeiten:** AI-002, GATE-001 gemerged
- **Parallel:** CU-002
- **Ziel:** Neutralen Claimtext und sichtbaren, begrenzten Plan erzeugen.
- **Umsetzung:**
  - Narrative ausschließlich aus `observed` und `user_stated` erzeugen.
  - Planner darf nur registrierte Tools auswählen und muss pro Schritt einen kurzen Grund liefern.
  - Toolargumente strikt validieren.
  - Safety-, Rechts-, Haftungs-, Kosten- und Submission-Aktionen im Plan blockieren.
  - Hauptfixture erzeugt genau eine Clarification.
- **Akzeptanz:** Eval-Fälle zeigen keine Schuldzuweisung oder erfundenen Werte; Toolplan enthält keine unbekannten/verbotenen Tools; deterministische Required-Field Engine bleibt maßgeblich.

### FE-003 – Evidence Board, Clarification und Event Stream

- **Owner:** Codex
- **Worktree:** Frontend Experience
- **Abhängigkeiten:** INT-001, stabile API-Verträge
- **Parallel:** AI-Tasks, Computer Use
- **Ziel:** Modellentscheidungen, Unsicherheit und Gate-Ergebnisse verständlich sichtbar machen.
- **Umsetzung:**
  - Evidence Cards mit Bildquelle, Status, Confidence und Unsicherheit.
  - Eine Clarification Card pro Runde.
  - SSE-basierter Event Strip für Plan, Tool, Gate, Fill und Verification.
  - Blocked-, Retry- und Emergency-Stati.
  - Keine Roh-Secrets oder vollständigen sensitiven Werte rendern.
- **Akzeptanz:** Mock- und Live-Events werden in Reihenfolge angezeigt; Nutzer erkennt Quelle jedes Portalwerts; Block-Grund ist ohne Logzugriff verständlich.

### FE-004 – Split View und Review Experience

- **Owner:** Codex
- **Worktree:** Frontend Experience
- **Abhängigkeiten:** FE-003, PORT-001
- **Parallel:** CU-Tasks
- **Ziel:** Portalbedienung und Claim-Provenienz gleichzeitig sichtbar machen.
- **Umsetzung:** Portalansicht links, Plan/Events/Provenienz rechts; Reviewtabelle mit Source, Status und Verifier-Ergebnis; unveränderliches Badge `Not submitted / human approval required`; Mismatch- und Repair-Darstellung.
- **Akzeptanz:** Desktop-Demo funktioniert bei 1280–1440 px ohne verdeckte Controls; Review ist auch ohne Event Logs verständlich; Approval-Grenze bleibt dauerhaft sichtbar.

### AUTH-001 – Agent-/Human-Trennung und G9/G10

- **Owner:** Codex, Security-Abnahme separat
- **Worktree:** Sandbox Portal
- **Abhängigkeiten:** INT-001
- **Parallel:** AI, Computer Use, Frontend
- **Ziel:** Agent kann strukturell keine Freigabe oder Receipt erzeugen.
- **Umsetzung:**
  - Getrennte Agent- und Human-Rollen/Tokens implementieren.
  - Human Approval erfordert One-Time-Token und Zustand `review`.
  - Agent-Token erhält immer `403`, auch bei direktem API-Aufruf.
  - Approval-Token darf nicht im Agent-Browser, DOM, Log oder Event Stream auftauchen.
  - Receipt nur nach Human Approval; konsequente Redaction.
- **Akzeptanz:** Unit- und Integrationstests belegen Rollen- und Zustandsgrenzen; Token-Reuse scheitert; Receipt vor Approval unmöglich.

### CU-001 – Isolierten Playwright-Computer-Use-Runner implementieren

- **Owner:** Codex
- **Worktree:** Computer Use
- **Abhängigkeiten:** INT-001, HUM-001
- **Parallel:** AI-002, FE-003, AUTH-001
- **Ziel:** GPT-5.6 darf ausschließlich das lokale Sandbox-Portal in einem isolierten Browser bedienen.
- **Umsetzung:**
  - Playwright-Chromium-Kontext pro Case starten und schließen.
  - Screenshot-/Action-Loop mit Responses API implementieren.
  - Domain-Allowlist auf lokale Portal-Origin beschränken.
  - Maximal 40 Aktionen, 90 Sekunden und definierte Timeouts.
  - Navigation außerhalb der Allowlist, neue Fenster und Downloads blockieren.
  - Runner beendet sich hart beim Zustand `review`.
- **Akzeptanz:** Portal A wird mit Mock-Paket semantisch ausgefüllt; externe URL wird blockiert; Timeout hinterlässt `blocked` und geschlossenen Browser; keine Approval-Aktion möglich.

### CU-002 – Tool Authority und Portal Write Gates G6/G7

- **Owner:** Codex
- **Worktree:** Computer Use
- **Abhängigkeiten:** CU-001, AI-003-Vertrag
- **Parallel:** VER-001 innerhalb des Worktrees danach
- **Ziel:** Tool- und Feldschreibrechte deterministisch kontrollieren.
- **Umsetzung:** Toolname, Argumente, CaseState, URL, Aktion und Limits vor jeder Ausführung validieren. Jeder Portalwert muss exakt aus dem freigegebenen ClaimPacket stammen und Provenienz besitzen. Unknown/not_supported, freie Modellwerte und fremde Felder blockieren.
- **Akzeptanz:** Negative Tests für unbekannte Tools, manipulierte Argumente, falsche States, fremde URLs, freie Werte und fehlende Provenienz bestehen; Gate kann nicht durch Portaltext überschrieben werden.

### VER-001 – Unabhängige Verifikation und G8

- **Owner:** Codex
- **Worktree:** Computer Use
- **Abhängigkeiten:** CU-002, Portal Read-only-Werte
- **Parallel:** EVAL-002
- **Ziel:** Portalwerte unabhängig frisch lesen und deterministisch gegen das ClaimPacket vergleichen.
- **Umsetzung:**
  - Frischen Screenshot und serverseitig gerenderte Werte erfassen.
  - Datum, Zeit und Whitespace normalisieren.
  - Feld-für-Feld- und Attachment-Vergleich durchführen.
  - Separaten Modell-Verifier nur als zusätzliche Blockquelle einsetzen.
  - Fault Injection erkennt falsches Feld, blockiert Review, repariert nur dieses Feld und verifiziert erneut.
- **Akzeptanz:** Kein deterministischer Mismatch kann vom Modell aufgehoben werden; bewusst falscher Wert und fehlender Anhang werden zu 100 % erkannt; Match-Report enthält pro Feld Expected, Actual, Status und Quelle.

### OBS-001 – Redigierte Events, Audit und Kostenmetriken

- **Owner:** Codex
- **Worktree:** Backend Workflow
- **Abhängigkeiten:** INT-001, Gate- und AI-Event-Verträge
- **Parallel:** AI, Computer Use, Frontend
- **Ziel:** Debugbarkeit und Demo-Transparenz ohne Datenleck.
- **Umsetzung:** SSE-Eventstore, Redaction-Funktion, Request-/Tooldauer, Retryanzahl, Modellkennung und geschätzte Nutzung speichern. Keine Bilder, Audiodaten, vollständigen Namen, Kennzeichen oder Policenwerte loggen.
- **Akzeptanz:** Snapshot-Tests der Redaction; Event-Reihenfolge stabil; Disconnect/Reconnect verliert keine abgeschlossenen Events; Logs bleiben bei Fehlern redigiert.

### EVAL-002 – Deterministische Grader und Eval-Runner

- **Owner:** Codex
- **Worktree:** Evals & Quality
- **Abhängigkeiten:** EVAL-001, INT-001
- **Parallel:** AI, Computer Use
- **Ziel:** Qualitätsregeln automatisch und kostenfrei gegen Fixtures prüfen.
- **Umsetzung:** Grader für Schema, Provenienz, verbotene Fakten, Required Fields, Safety Blocks, Toolnamen, Portalwerte, Mismatch, Approval und Receipt implementieren. Commands `make eval-deterministic` und maschinenlesbaren JSON-Report ergänzen.
- **Akzeptanz:** Bekannte absichtlich fehlerhafte Samples fallen mit korrektem ReasonCode durch; erfolgreiche Samples liefern 100-%-Metriken; Runner benötigt keinen OpenAI-Key.

### INT-002 – Vollständige End-to-End-Integration

- **Owner:** Mensch mit Codex-Unterstützung
- **Ausführung:** Lokaler `main`
- **Abhängigkeiten:** AI-001 bis AI-003, FE-003/004, AUTH-001, CU-001/002, VER-001, OBS-001, EVAL-002
- **Ziel:** Hauptfixture in zwei aufeinanderfolgenden Läufen von Intake bis verifiziertem Review bringen.
- **Integrationsreihenfolge:** Backend Events → AI → Portal Auth → Computer Use → Verifier → Frontend → Evals.
- **Akzeptanz:** Zwei vollständige Läufe ohne manuelle Datenkorrektur; genau eine Clarification; kein Approval durch Agent; Reset stellt identischen Ausgangszustand her. Falls nur feste Koordinaten funktionieren, wird Computer Use gemäß Kill Gate aus der Hauptdemo entfernt.

## 6. Welle 3 – Vollständige Evals, Sicherheit und Produktpolitur

### EVAL-003 – Dataset auf 24 Fälle vervollständigen

- **Owner:** Codex, Ground-Truth-Review durch Mensch
- **Worktree:** Evals & Quality
- **Abhängigkeiten:** INT-002, HUM-002
- **Parallel:** QA-001, DOC-002
- **Ziel:** Repräsentative Happy-, Edge- und Adversarial-Fälle vollständig abdecken.
- **Umsetzung:** Dataset auf 6 Happy, 4 Missing/Conflict, 4 Uncertain, 4 Safety, 3 Injection/Tool und 3 Portal/Mismatch/Approval ausbauen. Erwartete Fakten und verbotene Fakten manuell reviewbar darstellen.
- **Akzeptanz:** Genau 24 oder mehr valide Fälle; jede nichtdeterministische Stelle besitzt mindestens einen Positiv- und Negativfall; keine personenbezogenen Daten.

### EVAL-004 – Live-Evals, Model Grader und Reports

- **Owner:** Codex, Rubrikfreigabe durch Mensch
- **Worktree:** Evals & Quality
- **Abhängigkeiten:** EVAL-003, HUM-001
- **Parallel:** Security und QA
- **Ziel:** Modellqualität reproduzierbar messen, ohne Model Grader als Safety Gate zu verwenden.
- **Umsetzung:**
  - `make eval-live`, `make eval-safety`, `make eval-report` implementieren.
  - Rubrik für Neutralität, Faktentreue, Fragequalität, Plankürze und Unsicherheit mit 0–1 definieren.
  - Schwelle 0,85 pro Kategorie und kein Fall unter 0,70.
  - Deterministische Grader bleiben maßgeblich; Model Grader darf nur zusätzliche Fehler melden.
  - Markdown- und JSON-Regression Report erzeugen.
- **Akzeptanz:** Report trennt deterministic, model-graded und human-pending; Kosten und Laufzeit werden ausgewiesen; ein absichtlich parteiischer Narrative-Fall fällt durch.

### SEC-001 – Safety-, Injection- und Approval-Angriffe

- **Owner:** Codex; finale Risikoabnahme durch Mensch
- **Worktree:** Evals & Quality
- **Abhängigkeiten:** INT-002, AUTH-001, CU-002
- **Parallel:** EVAL-004, QA-001
- **Ziel:** Belegen, dass Prompt- und Portalinhalt keine Authority-Grenze verschieben.
- **Umsetzung:** Tests für Emergency, Injury, Liability, Payment, Real Portal, Tool Injection, Portal Prompt Injection, direkte API-Calls, Token-Reuse und 20 Approval-Angriffe. Einen Security-Review-Subagent nur read-only für zusätzliche Angriffsideen einsetzen.
- **Akzeptanz:** Safety-Block-Recall 100 % im kuratierten Set; 0 verbotene Toolaufrufe; 0 erfolgreiche Agent-Approvals; Receipt vor Human Approval 0 Fälle.

### QA-001 – Playwright-E2E für beide Portalvarianten

- **Owner:** Codex
- **Worktree:** Evals & Quality
- **Abhängigkeiten:** INT-002
- **Parallel:** SEC-001, DOC-002
- **Ziel:** Kritische Nutzer- und Agentenpfade browserbasiert absichern.
- **Umsetzung:** E2E für Intake, Clarification, Layout A/B, Mismatch/Repair, Safety Stop, Human Approval, Receipt und Reset. Deterministische Mock-Läufe in CI; Live-Computer-Use-Läufe als explizit gated Suite.
- **Akzeptanz:** CI-Suite ist stabil wiederholbar; beide Layoutvarianten erreichen mindestens 4/5 Live-Erfolge; Flaky Test wird nicht durch blindes Retry versteckt.

### QA-002 – Accessibility, Responsive und visuelle Politur

- **Owner:** Codex, visuelle Freigabe durch Mensch
- **Worktree:** Frontend Experience
- **Abhängigkeiten:** INT-002
- **Parallel:** Eval und Security
- **Ziel:** Kohärente Produktqualität statt technischer Demo-Sammlung.
- **Umsetzung:** Tastaturpfade, Fokus, Labels, Kontrast, Loading/Empty/Error/Blocked States, 1280–1440-px-Demoansicht und kleinere Viewports prüfen. Screenshots der Schlüsselschritte gegen visuelle Checkliste vergleichen.
- **Akzeptanz:** Keine kritischen Accessibility-Verstöße; kein überdeckter Demo-Control; Sandbox- und Human-Boundary-Hinweise auf Intake und Review sichtbar.

### PERF-001 – Zuverlässigkeit und 120-Sekunden-Ziel messen

- **Owner:** Codex
- **Worktree:** Evals & Quality
- **Abhängigkeiten:** INT-002
- **Parallel:** QA-002
- **Ziel:** Nachweisen, dass die Hauptdemo schnell und reproduzierbar genug ist.
- **Umsetzung:** Fünf vollständige Hauptläufe mit Laufzeit pro Phase, Actions, Retries und Kosten protokollieren. Langsame Phase identifizieren und nur risikolose Optimierungen vorschlagen.
- **Akzeptanz:** Mindestens 4/5 Läufe erreichen Review unter 120 Sekunden; Abweichungen sind im Report erklärt; kein Safety Gate wird für Geschwindigkeit abgeschwächt.

### DOC-002 – Finale technische Dokumentation

- **Owner:** Codex
- **Worktree:** Docs & Submission
- **Abhängigkeiten:** INT-002, reale Commands und verifizierte Ergebnisse
- **Parallel:** Evals und QA
- **Ziel:** Judges und Entwickler können das Projekt ohne mündliche Zusatzinformationen verstehen und starten.
- **Umsetzung:** README finalisieren; Architekturdiagramm, Setup, Start, Reset, Fixtures, erwartete Ausgabe, Troubleshooting, Codex-/GPT-5.6-Nutzung, Datenschutz, Security und Limitations dokumentieren. Nach HUM-008 die freigegebene Lizenzdatei ergänzen beziehungsweise den privaten Sharing-Weg dokumentieren. Nur gemessene Ergebnisse nennen.
- **Akzeptanz:** Clean-Checkout-Anleitung enthält alle Schritte; kein Platzhalter; keine unbewiesene Behauptung; Sample-Daten und Reset sind eindeutig.

## 7. Menschliche Produkt- und Submission-Aufgaben

### HUM-003 – Zwei externe Produkttests durchführen

- **Owner:** Mensch
- **Abhängigkeiten:** INT-002 und QA-002
- **Parallel:** EVAL-004, Dokumentation
- **Aufgabe:** Zwei Personen, die ClaimDone nicht gebaut haben, führen den Hauptflow ohne Hilfe aus. Sie bewerten Verständlichkeit, Vertrauen, Unsicherheit, Human Boundary, mentale Entlastung und Kohärenz von 1 bis 5.
- **Akzeptanz:** Median mindestens 4/5 pro Kategorie; niemand glaubt, ein echter Claim sei eingereicht worden; Beobachtungen werden als Fix oder bekannte Einschränkung dokumentiert.

### HUM-004 – Kill-Gate-Entscheidung treffen

- **Owner:** Mensch
- **Abhängigkeiten:** INT-002, erste Reliability-Ergebnisse
- **Aufgabe:** Wenn Computer Use nicht zweimal hintereinander semantisch funktioniert, nur feste Koordinaten benötigt oder die Human Boundary nicht beweisbar ist, auf Claim Packet Reviewer wechseln. Browserautomation dann nicht als funktionsfähig darstellen.
- **Akzeptanz:** Schriftliche Go/Fallback-Entscheidung; Scope danach eingefroren; keine neue Szenario- oder Hosting-Arbeit.

### HUM-005 – Demo-Video vorbereiten und veröffentlichen

- **Owner:** Mensch, Skript/Shotlist durch Codex
- **Abhängigkeiten:** Feature Freeze, bestandene Release-Kriterien
- **Aufgabe:** Unter drei Minuten langes öffentliches YouTube-Video mit Voiceover aufnehmen. Es zeigt Produkt, Hauptflow, Human Boundary sowie konkret, wie Codex und GPT-5.6 verwendet wurden. Haupt- und Ersatzaufnahme erstellen.
- **Akzeptanz:** Öffentlich abrufbar; unter drei Minuten; verständliche Sprache; kein reales sensibles Datum; Codex und GPT-5.6 werden hörbar erklärt.

### HUM-006 – `/feedback`-Session und Devpost-Daten sichern

- **Owner:** Mensch
- **Abhängigkeiten:** Kernimplementierung und Video
- **Aufgabe:** `/feedback` in der Session ausführen, in der der Großteil der Kernfunktionalität gebaut wurde. Submitter-Typ, Land, Kategorie, Repository-URL, Testhinweise und Video-URL vorbereiten.
- **Akzeptanz:** Session-ID und alle Pflichtantworten liegen außerhalb öffentlicher Logs sicher vor.

### HUM-007 – Live-Anforderungen prüfen und Submission autorisieren

- **Owner:** Mensch mit Devpost-Hackathons-Plugin
- **Abhängigkeiten:** REL-001
- **Aufgabe:** Direkt vor Video-Freeze und Submission Announcements und Submission Requirements live abrufen. Abweichungen zum lokalen Plan dokumentieren. Devpost-Update oder Submission erst nach ausdrücklicher menschlicher Freigabe ausführen.
- **Akzeptanz:** Aktuelle Requirements erfüllt; öffentliche/private Repository-Regel und Lizenz stimmen; keine Submission erfolgt allein aufgrund dieses Aufgabenplans.

## 8. Welle 4 – Release und Einreichungsbereitschaft

### REL-001 – Deterministisches Release Gate implementieren

- **Owner:** Codex
- **Worktree:** Evals & Quality oder dedizierter kurzer Worktree `codex/release-gate`
- **Abhängigkeiten:** EVAL-004, SEC-001, QA-001, PERF-001, DOC-002
- **Ziel:** Einen einzigen eindeutigen Pass/Fail-Entscheid für die abgabefähige Version erzeugen.
- **Umsetzung:** `make release-gate` prüft deterministische Tests, P0/Safety-Evals, Eval-Schwellen, Portal-Erfolgsquote, 20 Approval-Angriffe, Clean-Checkout, README, Lizenz, Fixtures und Testreport. Video und Session-ID werden als menschlich bestätigte Checkpoints geführt.
- **Akzeptanz:** Jeder fehlende Punkt liefert non-zero Exit und verständlichen ReasonCode; Model Grader kann kein deterministisches Fail überschreiben; Pass erzeugt versionierten ReleaseDecision-Report.

### REL-002 – Clean Checkout und finale Regression

- **Owner:** Codex, Ergebnis durch Mensch geprüft
- **Ausführung:** Frischer lokaler Checkout oder sauberer permanenter Integrations-Worktree
- **Abhängigkeiten:** REL-001, alle Integrationen gemerged
- **Ziel:** Belegen, dass das Repository außerhalb der Entwicklerumgebung reproduzierbar ist.
- **Umsetzung:** Ausschließlich README verwenden; Setup, Start, Hauptfixture, Reset, deterministische Evals und Release Gate ausführen. Keine untracked lokalen Hilfsdateien verwenden.
- **Akzeptanz:** Alles funktioniert aus sauberem Checkout; `git status` bleibt nach Tests erwartbar; Ergebnis und Commit-SHA werden im Testreport festgehalten.

### REL-003 – Feature Freeze und Abgabe-Build markieren

- **Owner:** Mensch
- **Abhängigkeiten:** REL-002, HUM-003 bis HUM-007
- **Aufgabe:** Finale Commit-SHA/Tag festlegen. Danach nur P0-Regressionsfixes; keine neue Integration, kein Hosting und kein zweites Schadensszenario.
- **Akzeptanz:** Release Gate grün; Video zeigt denselben Funktionsstand; Repo und Devpost-Texte widersprechen sich nicht; Zielabgabe ist 21. Juli 22:00 CEST.

## 9. Empfohlene Parallelisierung

### Parallelwelle A nach INT-000

Gleichzeitig starten:

1. Backend Workflow: BE-001.
2. Contracts & Gates: MEDIA-001 → GATE-001.
3. Frontend Experience: FE-001 → FE-002.
4. Sandbox Portal: PORT-001.

Read-heavy zusätzlich:

- Evals & Quality: EVAL-001.
- Docs & Submission: DOC-001.
- Mensch: HUM-001 und HUM-002.

Danach zwingend INT-001; keine Welle-2-Features auf auseinanderlaufenden Verträgen beginnen.

### Parallelwelle B nach INT-001

Gleichzeitig starten:

1. AI Workflow: AI-001 → AI-002 → AI-003.
2. Computer Use: CU-001 → CU-002 → VER-001.
3. Frontend Experience: FE-003 → FE-004.
4. Sandbox Portal: AUTH-001.

Zusätzlich:

- Backend Workflow: OBS-001.
- Evals & Quality: EVAL-002.

Danach zwingend INT-002 und zwei vollständige Hauptläufe.

### Parallelwelle C nach INT-002

Gleichzeitig:

1. Evals: EVAL-003 und EVAL-004.
2. Security/QA: SEC-001, QA-001, PERF-001.
3. Frontend: QA-002.
4. Dokumentation: DOC-002.
5. Mensch: HUM-003 und HUM-004.

Danach Release-Welle REL-001 → REL-002 → REL-003 sequenziell.

## 10. Promptvorlage für jeden Codex-Task

Jeder Task soll mit einer präzisen Übergabe gestartet werden:

```text
Implementiere Task <ID> aus CLAIMDONE_IMPLEMENTATION_TASKS.md.

Ausgangsbasis:
- Starte vom angegebenen Integrations-Commit.
- Lies AGENTS.md, CLAIMDONE_BUILD_WEEK_PLAN.md und den vollständigen Task.
- Arbeite nur im vorgesehenen Worktree und Branch codex/<name>.

Scope:
- Implementiere ausschließlich die unter „Umsetzung“ genannten Punkte.
- Halte das angegebene Datei-Eigentum ein.
- Bestehende Nutzeränderungen und fremde Worktree-Bereiche nicht verändern.
- Deterministische Gates dürfen nicht durch Modellurteile ersetzt werden.

Verifikation:
- Führe alle im Task relevanten Lint-, Typecheck- und Testkommandos aus.
- Ergänze negative Tests für Sicherheits- und Fehlerpfade.
- Wenn eine Voraussetzung fehlt, dokumentiere sie statt eine inkompatible Parallelstruktur zu erfinden.

Abschluss:
- Erstelle einen fokussierten Commit.
- Berichte Dateien, Tests, Ergebnisse, bekannte Risiken und Abhängigkeiten für den nächsten Task.
- Merge nicht selbst nach main.
```

## 11. Übergabe- und Integrationscheckliste

Jeder Codex-Agent muss am Ende liefern:

- Task-ID und Branchname.
- Kurze Verhaltensbeschreibung der Änderung.
- Liste der geänderten Dateien.
- Neue oder geänderte öffentliche Schnittstellen.
- Ausgeführte Commands und deren Ergebnis.
- Nicht ausgeführte Live-Tests mit Grund.
- Bekannte Risiken, TODOs und Folgeabhängigkeiten.
- Commit-SHA.
- Bestätigung, dass keine Secrets oder sensiblen Fixtures committed wurden.

Der Integrator prüft vor jedem Merge:

- Task-Scope eingehalten.
- Keine fremden Dateien unnötig verändert.
- Contracts nicht dupliziert.
- Deterministische Gates nicht abgeschwächt.
- Negative Tests vorhanden.
- Lockfiles kontrolliert regeneriert.
- Gesamtsuite nach Merge grün.
- Worktrees der nächsten Welle basieren auf aktualisiertem `main`.

## 12. Zusätzliche Umsetzungstipps

- **Integration ist der Engpass:** Bei einer menschlichen Person ist nicht Agentenkapazität, sondern Review und Merge der limitierende Faktor. Deshalb maximal vier aktive Schreib-Worktrees.
- **Verträge zuerst:** Frontend, Backend und Evals dürfen erst parallel starten, nachdem Contracts und Zustände gemerged sind.
- **Mocks früh, Live-Modelle gezielt:** Walking Skeleton und deterministische Tests mit Fake-Adaptern bauen; Live-GPT-5.6-Läufe nur an Integrations- und Eval-Checkpoints.
- **Ein Agent pro Verzeichnisverantwortung:** Nicht mehrere Agenten gleichzeitig Root-Manifeste, Contracts oder dieselben UI-Routen ändern lassen.
- **Subagents für Reviews:** Security, Testlücken, Accessibility und Log-Triage können innerhalb eines Worktree-Tasks parallel read-only geprüft werden.
- **Keine stillen Scope-Erweiterungen:** Hosting, zweites Schadensszenario, OCR und reale Versicherungsintegration bleiben außerhalb des MVP.
- **Nach jedem stabilen End-to-End-Lauf aufnehmen:** Kurze Arbeitsaufnahmen schützen vor späteren Regressionen und erleichtern das finale Video.
- **Fallback ehrlich halten:** Scheitert semantisches Computer Use, bleibt Claim Packet Reviewer ein valides Produkt; feste Koordinaten dürfen nicht als adaptive Automation präsentiert werden.
- **Devpost-Aktionen bleiben menschlich autorisiert:** Lesen und Prüfen darf Codex übernehmen; Update und Submission brauchen eine direkte, ausdrückliche Freigabe.
