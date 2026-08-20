# Google Photos Backup (Home Assistant Custom Component)

Automatisches Backup von Google Fotos/Videos auf eine lokal in Home Assistant
gemountete Festplatte. Installierbar über HACS (custom repository).

> **Wichtig, zuerst lesen:** Google hat am 31. März 2025 die Google Photos
> Library API stark eingeschränkt. `mediaItems.list`/`mediaItems.search`
> liefern seitdem **nur noch Medien, die von der aufrufenden App selbst
> erstellt/hochgeladen wurden** - nicht mehr die komplette, bestehende
> Bibliothek eines Nutzers. Das betrifft *alle* Drittanbieter-Tools, die
> über diese API gehen, auch `rclone`. Diese Integration wurde bewusst
> **mehrschichtig** gebaut, damit du trotzdem ein vollständiges Backup
> bekommst - lies unbedingt die Vergleichstabelle unten, bevor du ein
> Backend wählst.

## Architektur

```
custom_components/google_photos_backup/
├── __init__.py              # async_setup_entry/async_unload_entry, Services
├── application_credentials.py  # Google OAuth2 Endpunkte (nur library_api)
├── config_flow.py           # Backend-Auswahl + backend-spezifische Optionen
├── const.py
├── coordinator.py           # DataUpdateCoordinator, persistierter Sync-State
├── sensor.py                # last_sync, files_backed_up, last_error, free_space
├── services.yaml
├── strings.json / translations/{en,de}.json
└── backends/
    ├── base.py               # BackupBackend ABC, BackupStats, SyncStateStore
    ├── fsutil.py             # gemeinsame Datei-/Ordner-/Hash-Logik
    ├── library_api.py        # Library API (app-eigen) + Picker API
    ├── rclone_backend.py     # subprocess-Wrapper um `rclone`
    └── takeout_backend.py    # Takeout-Archiv-Import
```

Jedes Backend implementiert dasselbe `BackupBackend`-Interface
(`async_validate()`, `async_run_backup() -> BackupStats`). Der
`GooglePhotosBackupCoordinator` (ein `DataUpdateCoordinator`) kennt die
Backend-Details nicht - er lädt/speichert nur den persistierten Sync-State
(`homeassistant.helpers.storage.Store`) und ruft in festem Intervall
`backend.async_run_backup()` auf. Der Config Flow wählt beim Setup, welche
Backend-Klasse instanziiert wird (`backends/__init__.py:
async_create_backend`). Neue Backends lassen sich hinzufügen, ohne
Coordinator/Sensoren/Services anzufassen.

## Backend-Vergleich

| | **library_api** | **rclone** | **takeout** |
|---|---|---|---|
| Abdeckung bestehender Bibliothek | ❌ nur was diese Integration selbst hochlädt + manuelle Picker-Auswahl pro Sitzung | ❌ seit März 2025 nur was rclone selbst hochgeladen hat | ✅ vollständig (das ist der ganze Zweck von Takeout) |
| Automatisierungsgrad | Mittel (Picker braucht manuelle Nutzerinteraktion pro Auswahl-Sitzung) | Hoch (unbeaufsichtigt, aber inhaltlich leer für Bestandsfotos) | Mittel (Google generiert Exporte automatisch alle 2 Monate, aber Zustellung ins Watch-Verzeichnis ist ein separater Schritt) |
| EXIF-/Metadaten-Treue | Original-Datei direkt von Google (`=d`/`=dv`) | Original-Datei direkt von Google | Datei + JSON-Sidecar; Aufnahmedatum/mtime wird aus JSON übernommen, eingebettetes EXIF wird nicht neu geschrieben |
| Setup-Aufwand | Google-Cloud-Projekt + OAuth-Consent-Screen + Application Credentials | rclone-Binary + `rclone.conf` + OAuth im rclone-Kontext | Nur ein Watch-Verzeichnis; Google-Takeout-Export manuell oder per "geplante Exporte" |
| Abhängigkeit von externen Binaries | Keine | `rclone` muss separat bereitgestellt werden (nicht in HA OS enthalten) | Keine |
| Robustheit ggü. künftigen Google-API-Änderungen | Niedrig (API-Oberfläche kann jederzeit weiter eingeschränkt werden) | Niedrig (gleiche API im Hintergrund) | Hoch (Takeout ist ein Nutzerrecht nach DSGVO/Datenportabilität, kein API-Produkt das Google beliebig kappen kann) |

### Empfehlung

**Für ein vollständiges, dauerhaftes Backup einer bestehenden Bibliothek ist
das `takeout`-Backend die einzige Option, die das unter der aktuellen
API-Realität überhaupt leisten kann.** `library_api` und `rclone` sind
vollständig implementiert und funktionieren korrekt für das, was sie sehen
können - aber das ist strukturell nicht "die ganze Bibliothek", sondern nur
das, was über den jeweiligen OAuth-Client selbst hochgeladen bzw. per
Picker-Sitzung manuell ausgewählt wurde.

Für einen möglichst automatisierten Ablauf trotzdem: Google Takeout bietet
unter *"Geplante Exporte"* die Möglichkeit, alle 2 Monate für ein Jahr
automatisch einen neuen Google-Fotos-Export zu erzeugen und direkt in
Google Drive/Dropbox/OneDrive/Box abzulegen. Ein zweiter, von dieser
Integration unabhängiger Sync-Schritt (z. B. ein eigener rclone-Drive-Remote
oder Nextcloud) holt die Archive von dort in `takeout_watch_dir` - ab da
übernimmt diese Integration vollautomatisch Entpacken, Zuordnung und
Ablage. `library_api` (über den Picker-Service) eignet sich gut als
Ergänzung, um punktuell schnell aktuelle Fotos zu sichern, ohne auf den
nächsten 2-Monats-Export zu warten.

## Setup

### Gemeinsam: Installation über HACS

1. HACS → Custom repositories → `https://github.com/skornehl/ha-google-photos-backup` eintragen, Kategorie "Integration".
2. `Google Photos Backup` installieren, Home Assistant neu starten.
3. Einstellungen → Geräte & Dienste → Integration hinzufügen → "Google
   Photos Backup" → Backend wählen.

### Backend 1: library_api (Google Cloud OAuth)

1. In der [Google Cloud Console](https://console.cloud.google.com/) ein
   Projekt anlegen, **Photos Library API** und **Photos Picker API**
   aktivieren.
2. OAuth-Consent-Screen konfigurieren (External, Testnutzer = dein
   Google-Konto, solange die App nicht verifiziert ist).
3. OAuth-Client-ID (Typ "Web Application") erstellen, als Redirect-URI die
   von Home Assistant unter Einstellungen → Application Credentials
   vorgeschlagene URL eintragen (`https://DEIN_HA/auth/external/callback`).
4. In HA: Einstellungen → Application Credentials → Hinzufügen → Domain
   `google_photos_backup`, Client-ID/Secret eintragen.
5. Config Flow starten, Backend `library_api` wählen, Google-Konto
   autorisieren, Zielverzeichnis + Intervall setzen.
6. Um tatsächlich Fotos aus der **bestehenden** Bibliothek zu bekommen: den
   Service `google_photos_backup.start_picker_session` aufrufen, den Link
   aus der Benachrichtigung öffnen, Fotos/Alben auswählen. Der nächste
   Sync (oder `backup_now`) lädt die Auswahl herunter.

**Einschränkung:** ohne manuelle Picker-Auswahl passiert hier praktisch
nichts - siehe Vergleichstabelle.

### Backend 2: rclone

1. `rclone` muss im HA-Container/-Host verfügbar sein - **nicht** Teil von
   Home Assistant OS. Optionen: eigenes Add-on, das `rclone` bereitstellt
   und den Binary-Pfad über einen Bind-Mount sichtbar macht, oder ein
   angepasstes Core-Image. Ohne das bricht `async_validate()` beim Setup
   mit einer klaren Fehlermeldung ab.
2. Auf einem Rechner mit Browser: `rclone config`, Backend `google photos`
   wählen, durch den OAuth-Flow gehen. Die entstehende `rclone.conf` auf
   den HA-Host kopieren (Pfad im Config Flow angeben, oder Standardpfad
   nutzen).
3. Config Flow: Backend `rclone`, Remote-Name (wie in `rclone.conf`),
   Quellpfad (Standard `media/by-month`), Ziel, Intervall.

**Einschränkung:** liefert seit März 2025 nur Dateien, die über genau
diesen rclone-Remote hochgeladen wurden - siehe rclone-Doku
(`rclone can only download photos it uploaded`).

### Backend 3: Takeout (empfohlen)

1. [Google Takeout](https://takeout.google.com/) öffnen, nur "Google
   Fotos" auswählen, Exportformat `.zip`, Größe nach Bedarf begrenzen
   (kleinere Dateien = einfacher zu handhaben).
2. Optional, aber empfohlen: unter "Geplante Exporte" eine wiederkehrende
   Sicherung (alle 2 Monate, 1 Jahr) einrichten und als Ziel Drive/
   Dropbox/OneDrive/Box wählen.
3. Fertige Archive in das konfigurierte `takeout_watch_dir` legen (manuell,
   oder automatisiert über einen separaten Sync von dem Cloud-Ziel aus
   Schritt 2 - das ist bewusst nicht Teil dieser Integration).
4. Config Flow: Backend `takeout`, Zielverzeichnis, Watch-Verzeichnis,
   Intervall, optional "Archiv nach Import löschen".
5. Die Integration entpackt jedes neue Archiv, ordnet Medien per
   `<datei>.json`-Sidecar (`photoTakenTime`) chronologisch in
   `JJJJ/JJJJ-MM/` ein und überspringt bereits importierte Dateien
   (SHA-256-Hash-Vergleich, backend-übergreifend konsistent).

#### Große Bibliotheken: erster Voll-Export ohne Drive-Speicher

"Geplante Exporte" liefern *immer* nach Drive/Dropbox/OneDrive/Box - dafür
muss dort genug freier Speicher für die **komplette** Bibliothek vorhanden
sein (bei z. B. 1TB Fotos also ~1TB frei), da der erste Lauf alles
exportiert. Reicht das Kontingent nicht:

1. Für den **ersten** Export stattdessen einen **einmaligen** Takeout-Export
   mit Übermittlungsmethode **"Download-Link per E-Mail"** wählen (nicht
   "Zu Drive hinzufügen") - das zählt laut Google **nicht** gegen das
   Speicherkontingent, da die Archive nur befristet (~7 Tage, max. 5
   Downloads je Archiv) zum direkten Download bereitgestellt werden.
   50GB-Archivgröße wählen, um die Anzahl der Dateien klein zu halten.
2. Alle Archive innerhalb der 7 Tage herunterladen und ins
   `takeout_watch_dir` legen.
3. Erst **danach** auf "Geplante Exporte" (Drive) umstellen - die
   überträgt laut Google dann nur noch *neue/geänderte* Daten seit dem
   letzten Export, also für die meisten Bibliotheken nur noch wenige GB
   pro Lauf statt der vollen Größe.

Google Takeout erlaubt bei Fotos keine Auswahl nach Album oder Zeitraum -
ein einmaliger Export ist immer die komplette Bibliothek.

**Einschränkung:** kein Echtzeit-Sync - abhängig davon, wie oft Exporte
erzeugt und ins Watch-Verzeichnis geliefert werden. Kein automatisches
Neuschreiben von EXIF-Tags (nur Dateisystem-mtime/Ordnerstruktur werden aus
dem JSON abgeleitet).

## Services

- `google_photos_backup.backup_now(config_entry_id)` - sofortiger Sync-Lauf.
- `google_photos_backup.start_picker_session(config_entry_id)` - nur
  `library_api`; startet eine Picker-Sitzung und benachrichtigt mit dem
  Auswahl-Link.

## Bekannte Grenzen / offene Punkte

- Keine Wiederherstellungs-/Restore-Funktion, nur Backup-Richtung.
- `library_api`/`rclone` sind absichtlich vollständig implementiert (Spec
  verlangt sie), auch wenn ihr praktischer Nutzen für ein Voll-Backup unter
  der aktuellen Google-API-Politik gering ist - das kann sich ändern, falls
  Google die Einschränkungen wieder lockert.
- EXIF wird bei Takeout nicht neu in die Bilddatei geschrieben (nur
  Ordner/mtime); für Kamera-Originale meist irrelevant, da die Kamera das
  EXIF ohnehin schon korrekt gesetzt hat.
- Keine automatisierte Takeout-Auslösung per Browser-Automation - bewusst
  nicht umgesetzt (fragil, verstößt potenziell gegen Googles Nutzungs-
  bedingungen für automatisierte Zugriffe auf die Weboberfläche).
