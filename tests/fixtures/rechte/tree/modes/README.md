# modes

Eine Datei je Modus. Ein Modus legt fest, was die KI ohne Frage darf, was sie nur nach Rückfrage darf und was gesperrt ist. Er nennt dafür nur Nummern aus dem Katalog `rechte.md` im Wurzelordner. Die nächste Werkzeug-Benutzung gilt schon mit der geänderten Datei.

```text
modes/
├─ README.md     diese Datei
├─ _example.md   Vorlage: nach <name>.md kopieren
├─ auto.md
├─ plan.md
└─ bypass.md
```

Dateien mit `_` am Anfang zählen nicht. Der Dateiname ist der Name des Modus (`/mode auto`).

## Felder

| Feld | Bedeutung |
|---|---|
| `label`, `color` | Name und Farbe in der Statuszeile (`green`, `blue`, `red`, `yellow`, `purple`, `gray`) |
| `order` | Platz im Shift+Tab-Wechsel (1, 2, 3 ...). Ohne `order` nur mit `/mode` erreichbar |
| `regel` | `darf-nur`: nur die Nummern unter `rechte` sind erlaubt, alle anderen gesperrt. `darf-alles-ausser`: alles ist erlaubt außer den Nummern unter `rechte` |
| `rechte` | Nummern aus `rechte.md`: `[12, 13]`, mit Namen (`["13 Rechte-Dateien"]`) oder als Bereich (`["51-55"]`) |
| `fragt` | Nummern, die nur nach Rückfrage gehen (der dritte Zustand). Eine Nummer steht nur in einer der beiden Listen |
| `geheimnisse` | Geheimnisse aus `secrets/`, die der Modus freigibt: `[crm/api-1, telegram]` |
| `fragt-nie` | `ja`: der Modus fragt nie, was sonst fragen würde, ist erlaubt (Bypass immer) |
| `extra` | Feinschliff in der Regelschreibweise von `permissions.md`, z. B. `extra: {allow: ['web(domain:*.example.com)']}` |

Der Text unter dem Kopf geht als Modustext an die KI.

Je Nummer gilt also **erlaubt**, **fragt** oder **gesperrt**:

- **gesperrt** kann kein Agent und keine Sitzung aufheben.
- **fragt** fragt den Nutzer. Bei Nummern mit `Fragen: fest` im Katalog kann auch ein Agent die Frage nicht aufheben.
- **erlaubt** heißt: die Grundregeln von `permissions.md` gelten. Geheimnisse (31, 32) bleiben dabei zu, bis `geheimnisse:`, der Agent oder die Sitzung sie freigibt.

Steckt eine Nummer in einer größeren (Spalte `Teil von` in `rechte.md`, z. B. 13 in 12), gilt bei `darf-alles-ausser` der Zustand der größeren, solange die kleinere in keiner Liste steht: wer 12 sperrt, sperrt auch 13 und 14. Bei `darf-nur` ist jede nicht genannte Nummer gesperrt, auch eine kleinere.

Die festen Grenzen am Ende von `rechte.md` gelten in jedem Modus.

## Beispiele

Plan soll auch keine Webseiten abrufen:

```yaml
regel: darf-alles-ausser
rechte: [12, "51-55", 61, 63, 64, 24]
```

Ein Modus, der nur lesen und suchen darf, Shell nur nach Rückfrage:

```yaml
regel: darf-nur
rechte: [11, 24, 41, 44, 71, 72]
fragt: [21]
```

Auto soll vor dem Löschen fragen:

```yaml
fragt: [13, 22, 51, 52, 53]
```

## Modus anlegen

1. `_example.md` nach `<name>.md` kopieren.
2. `label`, `color`, `order`, `regel`, `rechte`, `fragt` und den Text setzen.
3. Nummer 13 (mit 14) unter `fragt` lassen oder sperren. Sonst kann sich ein Agent in diesem Modus selbst Rechte geben, und der Core liest `config.md` nicht mehr.
4. `lm check` zeigt Fehler in der Datei, `lm rules -m <name>` die Regeln, die daraus werden.

Eine Datei mit Fehler (unbekannte Nummer, kaputtes YAML) sperrt nur ihren eigenen Modus; die Fehlermeldung nennt den Grund.
