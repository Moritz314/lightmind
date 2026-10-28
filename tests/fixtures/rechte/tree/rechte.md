# Rechte

Alles, was ein Modus erlauben, sperren oder erfragen kann, mit einer festen Nummer. Die Modusdateien in `modes/` nennen nur diese Nummern. Wie man umschaltet, steht in `modes/README.md`.

Spalten: **Regeln** sind die Regeln, die der Core prüft (Schreibweise wie in `permissions.md`). **Grundsatz** sagt, was gilt, wenn der Modus die Nummer erlaubt: `offen` = erlaubt, `zu` = nur mit Freigabe (Agent, Sitzung oder `geheimnisse:` im Modus), `frage` = der Core fragt von sich aus; ohne Frage geht es nur in einem `darf-nur`-Modus, der die Nummer nennt. **Fragen** sagt, ob die Nummer unter `fragt:` stehen darf: `ja`, `fest` (die Frage kann kein Agent und keine Sitzung aufheben) oder `nein`. **Teil von**: die Nummer steckt in einer größeren und folgt ihr, solange ein Modus sie nicht selbst nennt (Einzelheiten in `modes/README.md`).

Nummern nicht umnummerieren: die Modusdateien verweisen darauf. Neue Rechte bekommen eine freie Nummer in ihrer Gruppe.

## 1 Dateien

| Nr | Recht | Regeln | Grundsatz | Fragen | Teil von |
|---|---|---|---|---|---|
| 11 | Dateien lesen | `read(**)` | offen | ja | |
| 12 | Dateien schreiben und ändern | `write(**)`, `edit(**)` | offen | ja | |
| 13 | Rechte-Dateien ändern (Freigaben, Agenten, Hooks, Trigger, Laufdaten; Liste `grant_files` in permissions.md) | `@grant_files` | offen | fest | 12 |
| 14 | Einstellungen ändern (config.md, wählt den Modus neuer Chats) | `write({config})`, `edit({config})` | offen | fest | 13 |

## 2 Shell, Server, Web

| Nr | Recht | Regeln | Grundsatz | Fragen | Teil von |
|---|---|---|---|---|---|
| 21 | Shell-Befehle ausführen | `bash(*)` | offen | ja | |
| 22 | Unlesbare Shell-Zeilen ausführen (z. B. Heredoc, offene Anführungszeichen) | `shell-unparsed` | frage | fest | |
| 23 | Server per ssh | `ssh(*)`, `server(*)` | offen | ja | |
| 24 | Webseiten abrufen | `web(*)` | offen | ja | |

## 3 Geheimnisse

| Nr | Recht | Regeln | Grundsatz | Fragen | Teil von |
|---|---|---|---|---|---|
| 31 | Geheimnisse benutzen | `secret(*)` | zu | ja | |
| 32 | Geheimnisse anlegen und ändern | `secret-write(*)` | zu | ja | |

## 4 Werkzeuge und Agenten

| Nr | Recht | Regeln | Grundsatz | Fragen | Teil von |
|---|---|---|---|---|---|
| 41 | Werkzeuge benutzen (aus tools/ und die eingebauten) | `tool(*)` | offen | ja | |
| 42 | Neue oder geänderte Werkzeuge benutzen (ohne bekannte Prüfsumme) | `new-tool(*)` | offen | ja | |
| 43 | Sub-Agenten starten | `spawn(*)` | offen | ja | |
| 44 | Skills und Werkzeug-Beschreibungen laden | `skill(*)` | offen | ja | |

## 5 Wirkung nach außen

| Nr | Recht | Regeln | Grundsatz | Fragen | Teil von |
|---|---|---|---|---|---|
| 51 | Geld ausgeben | `category(money)` | offen | ja | |
| 52 | Senden (Mail, Nachricht, an Kunden) | `category(send)` | offen | ja | |
| 53 | Löschen | `category(delete)` | offen | ja | |
| 54 | Veröffentlichen, deployen | `category(deploy)` | offen | ja | |
| 55 | Konten anlegen oder ändern | `category(account)` | offen | ja | |

## 6 Desktop und Browser

| Nr | Recht | Regeln | Grundsatz | Fragen | Teil von |
|---|---|---|---|---|---|
| 61 | PC steuern (Maus, Tastatur, Bildschirm) | `pc-control(*)` | offen | ja | |
| 62 | Browser bedienen | `browser(*)` | offen | ja | |
| 63 | Browser-Konten benutzen und auflisten (noch ohne Wirkung) | `browser-key(use)`, `browser-key(list)` | offen | ja | |
| 64 | Browser-Konten anlegen, Passwort erneuern (noch ohne Wirkung) | `browser-key(create)`, `browser-key(old)` | offen | ja | |

## 7 Helfer im Chat

| Nr | Recht | Regeln | Grundsatz | Fragen | Teil von |
|---|---|---|---|---|---|
| 71 | Dem Nutzer Fragen stellen (Werkzeug ask) | `tool(ask)` | offen | nein | 41 |
| 72 | To-do-Liste führen | `tool(todo)` | offen | nein | 41 |
| 73 | Timer stellen | `tool(timer)` | offen | ja | 41 |
| 74 | Hintergrund-Jobs und laufende Sub-Agenten steuern | `tool(job)`, `tool(relay)` | offen | ja | 41 |

## Feste Grenzen

Gelten in jedem Modus und lassen sich nicht umschalten. Die Regeln stehen in `permissions.md` (`locked:` oder `deny:`), `lm check` warnt, wenn eine fehlt. Was keine Regel hat, prüft der Core selbst.

| Kennung | Grenze | Regeln |
|---|---|---|
| F1 | Werte von Geheimnissen (.env) lesen oder schreiben | `read({secrets}/**/*.env)`, `write({secrets}/**/*.env)`, `edit({secrets}/**/*.env)`, `bash(*{secrets}/*.env*)` |
| F2 | Claude-Logins | `bash(*{claude}*)` |
| F3 | Motorraum `system/` lesen oder schreiben (nur der Agent chat-bot darf hinein) | `read({system}/**)`, `write({system}/**)`, `edit({system}/**)` |
| F4 | Unsichere Pfade und Adressen, kaputte Geheimnis-Verweise: immer abgelehnt | |
| F5 | Unlesbare Shell-Zeile, die Geheimnisse nennt: nie ohne Frage, in Bypass abgelehnt | |
| F6 | Bypass schreibt keine Rechte-Datei, die unter `fragt:` steht: abgelehnt statt erlaubt | |
| F7 | Vaultwarden direkt (Tresor, Datendateien, Dienst-Login) | `bash(*vaultwarden*)` |
| F8 | Schlüsseldienst direkt | `bash(*schluesseldienst*)` |
