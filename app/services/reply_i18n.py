"""Non-English reply vocabulary for the cold-outreach reply classifier.

``imap_reply_tracker`` classifies in a fixed order — bounce → automated →
refusal → explicit request → soft decline → weaker interest cue → neutral —
and its pattern lists were English-only. Cold lists are not: the live
mailbox answers in German, Danish, Spanish, Italian, French and Polish, and a
Polish "nie jestem zainteresowany" (not interested) was filed as *neutral*
while Spanish / Italian auto-responders reached the feed as ordinary replies.

Every list here is appended to the same-named English list in
``imap_reply_tracker`` (see the ``extend`` block after ``_POSITIVE_PATTERNS``),
so the ordering rules, the human-signal override and the conditional-offer
filter apply to these phrases exactly as they do to the English ones.

Precision rules (a wrong "automated" tag can blocklist a real prospect when
the auto-blocklist toggles are on):

* ``MACHINE_AUTO_PATTERNS`` hold only explicit "this is an automated
  message" / "do not reply" notices.
* ``ACK_PATTERNS`` hold *receipt or promise-to-answer* phrasing ("we have
  received your message", "we will reply shortly"), never a bare "thanks for
  your message" — a prospect says that too, and the English lists draw the
  same line.
* Spelling is accent-tolerant (``[ée]``, ``[ąa]`` …): replies typed on a
  phone or a foreign keyboard routinely drop diacritics.
"""

import re

_I = re.IGNORECASE


def _compile(*patterns: str) -> list[re.Pattern]:
    return [re.compile(p, _I) for p in patterns]


# ── Automated mail ───────────────────────────────────────────────────────────

# Explicit machine notices — decisive, never overridden by a human signal.
MACHINE_AUTO_PATTERNS = _compile(
    # German
    r"\bdies\s+ist\s+eine\s+automatisch\w*\s+(?:antwort|nachricht|e-?mail|benachrichtigung|best[äa]tigung)\b",
    r"\bbitte\s+antworten\s+sie\s+nicht\s+auf\s+diese\s+(?:e-?mail|nachricht)\b",
    # French
    r"\bceci\s+est\s+un\s+(?:message|mail|courriel|e-?mail)\s+automatique\b",
    r"\bmessage\s+g[ée]n[ée]r[ée]\s+automatiquement\b",
    r"\bne\s+pas\s+r[ée]pondre\s+[àa]\s+ce\s+(?:message|mail|courriel|e-?mail)\b",
    # Spanish
    r"\best[ea]\s+es\s+un\s+(?:mensaje|correo|e-?mail|email)\s+autom[áa]tico\b",
    r"\b(?:mensaje|correo)\s+generado\s+autom[áa]ticamente\b",
    r"\bno\s+responda\s+a\s+est[ea]\s+(?:mensaje|correo)\b",
    # Italian
    r"\bquest[ao]\s+[èe]\s+una\s+(?:mail|e-?mail|email|risposta|comunicazione|notifica)\s+automatic[ao]\b",
    r"\bmessaggio\s+generato\s+automaticamente\b",
    r"\bnon\s+rispondere\s+a\s+quest[ao]\s+(?:mail|e-?mail|messaggio)\b",
    # Portuguese
    r"\best[ae]\s+[ée]\s+uma\s+(?:mensagem|resposta|e-?mail)\s+autom[áa]tica\b",
    r"\bn[ãa]o\s+responda\s+(?:a\s+)?est[ea]\s+(?:e-?mail|mensagem)\b",
    # Dutch
    r"\bdit\s+is\s+een\s+automatisch\w*\s+(?:bericht|antwoord|e-?mail|reactie)\b",
    r"\bdeze\s+e-?mail\s+is\s+automatisch\s+gegenereerd\b",
    # Polish
    r"\bto\s+jest\s+automatyczn\w+\s+(?:odpowied[źz]|wiadomo[śs][ćc]|powiadomienie)\b",
    r"\bwiadomo[śs][ćc]\s+(?:zosta[łl]a\s+)?wygenerowana\s+automatycznie\b",
    # Danish / Norwegian / Swedish
    r"\bdett?e\s+er\s+en\s+automatisk\w*\s+(?:besked|svar|melding|e-?mail|e-?post)\b",
    r"\bdet\s+h[äa]r\s+[äa]r\s+ett\s+automatiskt\w*\s+(?:svar|meddelande|mail|mejl)\b",
)

# Shared-mailbox acknowledgements: receipt of our mail or a promise to answer.
# Machine kind, but body wording — so a human who also writes "send pricing"
# in the same message stays a human.
ACK_PATTERNS = _compile(
    # German
    r"\bwir\s+haben\s+(?:ihre|deine)\s+(?:nachricht|anfrage|e-?mail|mail)\s+(?:erhalten|empfangen)\b",
    r"\b(?:ihre|deine)\s+(?:nachricht|anfrage|e-?mail|mail)\s+(?:ist|wurde)\s+(?:bei\s+uns\s+)?"
    r"(?:eingegangen|erhalten|weitergeleitet)\b",
    r"\bwir\s+(?:melden\s+uns|antworten(?:\s+ihnen)?|werden\s+uns\s+(?:bei\s+ihnen\s+)?melden)\b[^.\n]{0,40}"
    r"\b(?:schnellstm[öo]glich|zeitnah|so\s+bald|baldm[öo]glichst|innerhalb)\b",
    # French
    r"\bnous\s+avons\s+bien\s+re[çc]u\s+(?:votre|ton|ta)\b",
    r"\bnous\s+accusons\s+r[ée]ception\b",
    r"\bvotre\s+(?:message|demande|e-?mail|courriel|mail)\s+a\s+bien\s+[ée]t[ée]\s+(?:re[çc]u|transmis|enregistr[ée])",
    r"\bnous\s+(?:reviendrons\s+vers\s+vous|vous\s+r[ée]pondrons|vous\s+recontacterons)\b[^.\n]{0,40}"
    r"\b(?:meilleurs\s+d[ée]lais|rapidement|au\s+plus\s+vite|bient[ôo]t|sous\s+\d)",
    # Spanish
    r"\b(?:te|le|les)\s+(?:responderemos|contestaremos|atenderemos|contactaremos|escribiremos)\b[^.\n]{0,30}"
    r"\b(?:brevedad|antes\s+posible|\d+\s+horas|\d+\s+d[íi]as)",
    r"\bnos\s+pondremos\s+en\s+contacto\b",
    r"\bhemos\s+recibido\s+(?:tu|su|vuestro)\s+(?:mensaje|correo|consulta|solicitud|petici[óo]n|e-?mail|email)\b",
    # Italian
    r"\bla\s+(?:sua|tua|vostra)\s+richiesta\s+[èe]\s+(?:stata\s+)?"
    r"(?:ricevuta|presa\s+in\s+carico|in\s+fase\s+di\s+lavorazione|in\s+lavorazione)\b",
    r"\b(?:le|ti|vi)\s+(?:risponderemo|ricontatteremo)\b",
    r"\bricever[àa]\s+una\s+risposta\b",
    r"\babbiamo\s+ricevuto\s+(?:la\s+sua|la\s+tua|il\s+suo|il\s+tuo)\s+(?:richiesta|messaggio|mail|e-?mail)\b",
    # Portuguese
    r"\brecebemos\s+(?:a\s+sua|a\s+tua|o\s+seu|o\s+teu|sua|seu)\s+"
    r"(?:mensagem|e-?mail|pedido|solicita[çc][ãa]o|contato|contacto)\b",
    r"\b(?:entraremos\s+em\s+contacto|entraremos\s+em\s+contato|responderemos|retornaremos)\b[^.\n]{0,40}"
    r"\b(?:breve|brevemente|prazo|horas|dias)\b",
    # Dutch
    r"\b(?:wij|we)\s+hebben\s+(?:uw|je)\s+(?:bericht|e-?mail|aanvraag|vraag)\s+(?:goed\s+)?ontvangen\b",
    r"\bwe\s+(?:reageren|antwoorden|nemen\s+contact\s+met\s+u\s+op)\b[^.\n]{0,30}\b(?:zo\s+snel|binnen|spoedig)",
    # Polish
    r"\botrzymali[śs]my\s+(?:twoj[ąa]|pa[ńn]stwa|wasz[ąa])\s+(?:wiadomo[śs][ćc]|zapytanie|zg[łl]oszenie)\b",
    r"\bodpowiemy\b[^.\n]{0,40}\b(?:najszybciej|wkr[óo]tce|w\s+ci[ąa]gu|jak\s+tylko)\b",
    r"\bskontaktujemy\s+si[ęe]\b[^.\n]{0,40}\b(?:wkr[óo]tce|najszybciej|w\s+ci[ąa]gu)\b",
    # Danish / Norwegian / Swedish
    r"\bvi\s+har\s+modtaget\s+(?:din|jeres)\s+(?:henvendelse|besked|mail|e-?mail|foresp[øo]rgsel)\b",
    r"\bskal\s+hermed\s+bekr[æe]fte,?\s+at\s+vi\s+har\s+modtaget\b",
    r"\bvi\s+har\s+mottatt\s+(?:din|deres)\s+(?:henvendelse|melding|e-?post|foresp[øo]rsel)\b",
    r"\bvi\s+har\s+tagit\s+emot\s+(?:ditt|ert|din)\s+(?:meddelande|mejl|e-?post|f[öo]rfr[åa]gan|[äa]rende)\b",
)

# Dead-end addresses: the mailbox no longer exists or is not read (machine
# kind, like the English departure notices). English variants live here too —
# "this email address is no longer in service" matched nothing.
#
# Every phrase must sit next to a mailbox word: a bare "no longer available /
# active / used" also describes a product, an offer or a vendor a prospect is
# talking about, and a machine tag can blocklist a real person.
_MAILBOX_WORD = (
    r"(?:e-?mail|address|mailbox|inbox|account|adresse|bo[iî]te|messagerie|courriel|compte|"
    r"direcci[óo]n|correo|cuenta|buz[óo]n|indirizzo|casella|conta|endere[çc]o|"
    r"postfach|konto|adres|skrzynk\w*|postvak)"
)


def _dead_address(core: str) -> re.Pattern:
    return re.compile(rf"{_MAILBOX_WORD}\b[^.\n]{{0,60}}?\b{core}", _I)


DEPARTURE_PATTERNS = [
    _dead_address(r"(?:is\s+)?no\s+longer\s+(?:in\s+(?:service|use)|active|monitored|valid|exist\w*|used|checked)\b"),
    _dead_address(r"(?:is\s+)?not\s+(?:in\s+use|active|valid|monitored|checked|attended)\b"),
    _dead_address(r"(?:has\s+been|was)\s+(?:closed|disabled|deactivated|deleted)\b"),
    _dead_address(r"n'est\s+plus\s+(?:en\s+service|utilis[ée]e?|valide|active|consult[ée]e?)\b"),
    _dead_address(r"ya\s+no\s+(?:est[áa]\s+(?:en\s+servicio|activ[oa]|en\s+uso)|existe|se\s+(?:utiliza|usa|revisa))\b"),
    _dead_address(r"(?:wird\s+nicht\s+mehr\s+(?:genutzt|verwendet|gelesen|betreut|[üu]berwacht|ueberwacht)"
                  r"|existiert\s+nicht\s+mehr|ist\s+nicht\s+mehr\s+(?:in\s+betrieb|aktiv|g[üu]ltig|erreichbar))\b"),
    _dead_address(r"non\s+[èe]\s+pi[ùu]\s+(?:attiv[oa]|in\s+uso|valid[oa]|monitorat[oa])\b"),
    _dead_address(r"n[ãa]o\s+est[áa]\s+mais\s+(?:em\s+uso|ativ[oa]|v[áa]lid[oa])\b"),
    _dead_address(r"wordt\s+niet\s+meer\s+(?:gebruikt|gelezen|beheerd)\b"),
    _dead_address(r"nie\s+jest\s+ju[żz]\s+(?:aktywn\w+|u[żz]ywan\w+|monitorowan\w+)\b"),
]

# Out-of-office wording the English list lacks (Polish and Nordic variants).
OOO_PATTERNS = _compile(
    r"\bpoza\s+biurem\b",
    r"\b(?:jestem|b[ęe]d[ęe])\s+nieobecn\w+",
    r"\bprzebywam\s+na\s+urlopie\b",
    r"\bude\s+af\s+(?:kontoret|huset)\b|\bute\s+av\s+kontoret\b|\bborte\s+fra\s+kontoret\b",
)


# ── Refusals ─────────────────────────────────────────────────────────────────

# Decisive refusals / opt-out requests.
NEGATIVE_PATTERNS = _compile(
    # German
    r"\bkein(?:e|en|er)?\s+interesse\b",
    r"\bnicht\s+(?:daran\s+)?interessiert\b",
    r"\b(?:bitte\s+keine|keine\s+weiteren?)\s+(?:e-?mails?|mails?|werbung|nachrichten|angebote|zusendungen|kontaktaufnahme)\b",
    r"\buninteressiert\b",
    r"\b(?:entfernen|streichen|l[öo]schen|nehmen)\s+sie\s+(?:mich|uns|meine|unsere|meinen|unseren)\b",
    r"\bnicht\s+mehr\s+(?:kontaktieren|anschreiben|anrufen|bel[äa]stigen)\b",
    r"\bnein\s*,?\s+danke\b",
    r"\b(?:melden\s+sie\s+(?:mich|uns)\s+ab|austragen|abbestellen)\b",
    # French
    r"\bpas\s+int[ée]ress[ée]e?s?\b",
    r"\bne\s+(?:m'|nous\s+)?(?:envoyez|contactez|[ée]crivez|sollicitez|d[ée]rangez)\s+plus\b",
    r"\b(?:non\s+merci|merci\s*,?\s+non)\b",
    r"\bd[ée]sinscri\w+|\bd[ée]sabonn\w+",
    r"\b(?:retirez|supprimez|enlevez)[\s-]+(?:moi|nous|mon|notre)\b",
    # Spanish
    r"\bno\s+(?:me\s+|nos\s+)?interesa(?:n)?\b",
    r"\bno\s+(?:estoy|estamos)\s+interesad[oa]s?\b",
    r"\bno,?\s+gracias\b",
    r"\bno\s+(?:me|nos)\s+(?:escrib(?:a|an)|contact(?:e|en)|env[íi](?:e|en)|mand(?:e|en))\b",
    r"\bdeje(?:n)?\s+de\s+(?:escribir|enviar|contactar|mandar)\w*",
    r"\b(?:dar|darme|darnos|d[ée]nos|d[ée]me|darse)\s+(?:de\s+)?baja\b",
    r"\bdesuscrib\w+|\bdesuscripci[óo]n\b",
    # Italian
    r"\bnon\s+(?:siamo|sono|ci|mi)\s+interessat\w*",
    r"\bnon\s+(?:ci\s+|mi\s+)?interessa(?:no)?\b",
    r"\bno\s*,?\s+grazie\b",
    r"\bnon\s+(?:ci\s+|mi\s+)?(?:contattat\w*|scrivet\w*|inviat\w*|mandat\w*|disturbat\w*)\b",
    r"\b(?:cancell|rimuov|elimin|togli)\w*\s+(?:pure\s+)?(?:il\s+mio|la\s+mia|il\s+nostro|la\s+nostra)\b",
    r"\bdisiscri\w+",
    # Portuguese
    r"\bn[ãa]o\s+(?:estou|estamos|tenho|temos)\s+(?:interessad[oa]s?|interesse)\b",
    r"\bn[ãa]o\s+(?:me\s+|nos\s+)?interessa(?:m)?\b",
    r"\bsem\s+interesse\b",
    r"\bn[ãa]o,?\s+obrigad[oa]\b",
    r"\bn[ãa]o\s+(?:me\s+|nos\s+)?(?:envie|contacte|contate|escreva|mande)\w*",
    # Dutch
    r"\bgeen\s+interesse\b",
    r"\bniet\s+ge[ïi]nteresseerd\b",
    r"\bnee\s*,?\s+(?:bedankt|dank\s*u)\b",
    r"\bverwijder\s+(?:mij|ons|mijn)\b",
    r"\bniet\s+meer\s+(?:mailen|e-?mailen|contacteren|benaderen)\b",
    r"\buitschrijven\b",
    # Polish
    r"\bnie\s+(?:jestem|jeste[śs]my)\s+zainteresowan\w*",
    r"\bnie\s+interesuj(?:e|[ąa])\s+(?:nas|mnie)\b|\bnie\s+jest\s+to\s+dla\s+nas\b",
    r"\bprosz[ęe]\s+nie\s+(?:pisa[ćc]|wysy[łl]a[ćc]|kontaktowa[ćc]|dzwoni[ćc])\w*",
    r"\bprosz[ęe]\s+(?:o\s+)?(?:usuni[ęe]cie|wypisanie|skre[śs]lenie)\b",
    r"\bnie\s+(?:chc[ęe]|chcemy)\s+(?:otrzymywa[ćc]|dostawa[ćc])\b",
    r"\b(?:wypisa[ćc]|usu[ńn])\s+(?:mnie|nas)\b",
    # Danish / Norwegian / Swedish
    r"\bikke\s+interesser(?:et|ede|erede)\b",
    r"\bikke\s+interessert\b",
    r"\binte\s+intresserad(?:e)?\b",
    r"\bingen\s+interesse\b|\binget\s+intresse\b",
    r"\bnej\s+tak\b|\bnej\s+tack\b|\bnei\s*,?\s+takk\b",
    r"\bslet\s+(?:mig|os)\b|\bslett\s+(?:meg|oss)\b|\bafmeld\w*|\bavregistrera\b",
)

# Polite declines — no blunt refusal, but no interest either. Like the English
# soft list, these yield to an explicit request in the same reply.
SOFT_NEGATIVE_PATTERNS = _compile(
    # German
    r"\bkein(?:e|en)?\s+(?:bedarf|budget|kapazit[äa]t(?:en)?)\b",
    r"\bsind\s+(?:bereits|schon)\s+(?:gut\s+)?(?:versorgt|ausgestattet|aufgestellt)\b",
    r"\bhaben\s+(?:bereits|schon)\s+(?:einen|eine|ein)\s+\w+\s*(?:dienstleister|partner|agentur|anbieter|lieferanten)\b",
    r"\b(?:derzeit|momentan|aktuell|zur\s+zeit)\s+(?:kein|nicht|keinen)\b",
    # French
    r"\bpas\s+(?:de\s+)?(?:besoin|budget)\b",
    r"\bnous\s+avons\s+d[ée]j[àa]\b",
    r"\bpas\s+(?:pour\s+le\s+moment|actuellement)\b",
    # Spanish
    r"\bno\s+(?:tenemos|tengo)\s+(?:necesidad|presupuesto)\b",
    r"\bno\s+(?:lo\s+)?necesit(?:amos|o)\b",
    r"\bya\s+(?:tenemos|contamos\s+con)\b",
    r"\b(?:por\s+ahora|por\s+el\s+momento|ahora)\s+no\b",
    # Italian
    r"\bnon\s+(?:abbiamo|ho)\s+(?:bisogno|necessit[àa]|budget)\b",
    r"\bgi[àa]\s+(?:abbiamo|collaboriamo|lavoriamo)\b",
    r"\b(?:per\s+il\s+momento|al\s+momento)\s+no\b|\bnon\s+al\s+momento\b",
    # Portuguese
    r"\bn[ãa]o\s+(?:temos|tenho)\s+(?:necessidade|or[çc]amento)\b",
    r"\bj[áa]\s+(?:temos|trabalhamos\s+com)\b",
    r"\bpor\s+agora\s+n[ãa]o\b",
    # Dutch
    r"\bgeen\s+(?:behoefte|budget)\b",
    r"\bwij\s+hebben\s+al\b|\bal\s+een\s+(?:partner|leverancier)\b",
    # Polish
    r"\bnie\s+potrzebuj\w+",
    r"\bnie\s+mamy\s+(?:bud[żz]etu|potrzeby)\b",
    r"\bju[żz]\s+wsp[óo][łl]pracujemy\b|\bmamy\s+ju[żz]\b",
    # Danish / Norwegian / Swedish
    r"\bikke\s+brug\s+for\b|\bhar\s+ikke\s+behov\b|\bhar\s+inte\s+behov\b|\bingen\s+behov\b|\bikke\s+aktuelt\b",
    r"\bvi\s+har\s+allerede\b|\bhar\s+redan\b",
)


# ── Interest ─────────────────────────────────────────────────────────────────

# Explicit statements of interest or requests. Evaluated after the refusals,
# so "nicht interessiert" / "no estoy interesado" never reach these.
STRONG_POSITIVE_PATTERNS = _compile(
    # German
    r"\binteressiert\b",
    r"\b(?:haben|h[äa]tten|habe|h[äa]tte)\s+(?:gro(?:ß|ss)es\s+|durchaus\s+|generell\s+|grunds[äa]tzlich\s+)?interesse\b",
    r"\b(?:schicken|senden)\s+sie\s+(?:uns|mir)\b",
    r"\bklingt\s+(?:gut|interessant|spannend)\b",
    r"\bk[öo]nnen\s+wir\s+(?:telefonieren|sprechen|uns\s+austauschen)\b",
    # French
    r"\bint[ée]ress[ée]e?s?\b",
    r"\b(?:[çc]a|cela)\s+(?:m'|nous\s+)?int[ée]resse\b",
    r"\bpouvez[\s-]vous\s+(?:m'|nous\s+)?(?:envoyer|transmettre|pr[ée]senter)\b",
    r"\benvoyez[\s-]+(?:moi|nous)\b",
    r"\b(?:appelez|contactez)[\s-]+(?:moi|nous)\b",
    r"\bparlons[\s-]en\b",
    # Spanish
    r"\b(?:me|nos)\s+interesa\b",
    r"\binteresad[oa]s?\b",
    r"\b(?:env[íi]a|env[íi]e|env[íi]en|m[áa]nda|m[áa]nde|m[áa]nden)(?:me|nos)\b",
    r"\bcu[ée]ntame\s+m[áa]s\b",
    r"\bagend(?:emos|ar)\s+una\s+(?:llamada|reuni[óo]n)\b",
    r"\b(?:ll[áa]ma|ll[áa]men)(?:me|nos)\b",
    r"\bme\s+gustar[íi]a\s+(?:saber|conocer|recibir|ver)\b",
    r"\bsuena\s+(?:bien|interesante)\b",
    r"\bpodemos\s+(?:agendar|programar|hablar|coordinar)\b",
    # Italian
    r"\binteressat[oaie]\b",
    r"\b(?:mi|ci)\s+interessa\b",
    r"\b(?:inviat|mandat)\w*(?:mi|ci)\b",
    r"\b(?:ulteriori|maggiori|pi[ùu])\s+informazioni\b",
    r"\bparliamone\b",
    r"\bci\s+piacerebbe\b",
    r"\bfissiamo\s+(?:una\s+)?(?:chiamata|call|riunione)\b",
    r"\bpossiamo\s+(?:sentirci|vederci|parlarne|fissare)\b",
    # Portuguese
    r"\binteressad[oa]s?\b",
    r"\bme\s+interessa\b",
    r"\b(?:envie|mande)[\s-]?(?:me|nos)\b",
    r"\bmais\s+informa[çc][õo]es\b",
    r"\bgostar[íi]amos\s+de\b",
    # Dutch
    r"\bge[ïi]nteresseerd\b",
    r"\bstuur\s+(?:mij|ons|me)\b",
    r"\bmeer\s+informatie\b",
    # Polish
    r"\bzainteresowan\w*",
    r"\bprosz[ęe]\s+o\s+(?:przes[łl]anie|ofert[ęe]|wycen[ęe]|kontakt)\b",
    r"\bprosz[ęe]\s+(?:przes[łl]a[ćc]|wys[łl]a[ćc])\b",
    r"\bch[ęe]tnie\s+(?:si[ęe]\s+)?(?:dowiem|us[łl]ysz|porozmawiam|spotkam)",
    r"\bwi[ęe]cej\s+informacji\b",
    # Danish / Norwegian / Swedish
    r"\binteresseret\b|\binteressert\b|\bintresserad(?:e)?\b",
    r"\bsend\s+(?:gerne\s+)?(?:mig|os|meg|mere)\b|\bskicka\s+(?:g[äa]rna\s+)?(?:mig|oss|mer)\b",
    r"\bmere\s+information\b|\bmer\s+information\b",
)

# Weaker interest cues (price / call talk), same role as the English list.
POSITIVE_PATTERNS = _compile(
    r"\bwas\s+kostet\b|\bwie\s+viel\s+kostet\b",
    r"\bcombien\s+(?:[çc]a\s+)?co[uû]te\b|\bquel\s+(?:est\s+)?(?:votre\s+)?tarif\b",
    r"\bcu[áa]nto\s+(?:cuesta|cobr\w+)\b|\bqu[ée]\s+precios?\b",
    r"\bquanto\s+costa\b|\bpreventivo\b",
    r"\bile\s+kosztuje\b",
    r"\bwat\s+kost\b|\bwat\s+zijn\s+de\s+kosten\b",
)

# Promotional footers phrase a third-person offer in the sender's language
# ("Wenn Sie interessiert sind, besuchen Sie …"). The keyword inside belongs
# to the sender's marketing copy, not to a prospect's interest — the same
# trap as the English "if you are interested" footer.
CONDITIONAL_OFFER_RE = re.compile(
    r"(?:wenn\s+sie\s+interessiert\s+sind|falls\s+sie\s+interessiert|bei\s+interesse"
    r"|si\s+vous\s+[êe]tes\s+int[ée]ress[ée]|si\s+cela\s+vous\s+int[ée]resse"
    r"|si\s+(?:est[áa]s?|usted\s+est[áa])\s+interesad|si\s+te\s+interesa|si\s+le\s+interesa"
    r"|se\s+(?:sei|[èe]|siete)\s+interessat|se\s+ti\s+interessa|se\s+le\s+interessa"
    r"|se\s+estiver\s+interessad|als\s+(?:u|je)\s+ge[ïi]nteresseerd"
    r"|je[śs]li\s+jest(?:e[śs])?\s+zainteresowan|hvis\s+du\s+er\s+interesseret"
    r"|om\s+du\s+[äa]r\s+intresserad)[^.!?\n]*[.]?",
    _I,
)

# A human signal in any language: used to stop an acknowledgement / away
# pattern from swallowing a real answer ("Gracias por tu mensaje, nos interesa").
#
# Deliberately NARROW, like the English override: only an explicit statement
# of interest or a refusal. Calls to action ("contactez-moi", "llámame",
# "senden Sie mir …") are what vacation notices say about urgent matters, and
# "désinscrire" / "austragen" are what footers say — neither may turn an
# automated message into a person.
HUMAN_OVERRIDE_PATTERNS = _compile(
    # Interest
    r"\binteressiert\b",
    r"\b(?:haben|h[äa]tten|habe|h[äa]tte)\s+(?:gro(?:ß|ss)es\s+|durchaus\s+)?interesse\b",
    r"\bklingt\s+(?:gut|interessant|spannend)\b",
    r"\bint[ée]ress[ée]e?s?\b|\b(?:[çc]a|cela)\s+(?:m'|nous\s+)?int[ée]resse\b",
    r"\b(?:me|nos)\s+interesa\b|\binteresad[oa]s?\b|\bsuena\s+(?:bien|interesante)\b",
    r"\binteressat[oaie]\b|\b(?:mi|ci)\s+interessa\b|\bci\s+piacerebbe\b",
    r"\bme\s+interessa\b|\bge[ïi]nteresseerd\b|\bzainteresowan\w*",
    r"\binteresseret\b|\binteressert\b|\bintresserad(?:e)?\b",
    # Refusals
    r"\bkein(?:e|en|er)?\s+interesse\b|\bnicht\s+(?:daran\s+)?interessiert\b|\bnein\s*,?\s+danke\b",
    r"\bpas\s+int[ée]ress[ée]e?s?\b|\b(?:non\s+merci|merci\s*,?\s+non)\b",
    r"\bno\s+(?:me\s+|nos\s+)?interesa(?:n)?\b|\bno\s+(?:estoy|estamos)\s+interesad[oa]s?\b|\bno,?\s+gracias\b",
    r"\bnon\s+(?:siamo|sono|ci|mi)\s+interessat\w*|\bnon\s+(?:ci\s+|mi\s+)?interessa(?:no)?\b|\bno\s*,?\s+grazie\b",
    r"\bn[ãa]o\s+(?:estou|estamos|tenho|temos)\s+(?:interessad[oa]s?|interesse)\b|\bn[ãa]o,?\s+obrigad[oa]\b",
    r"\bgeen\s+interesse\b|\bniet\s+ge[ïi]nteresseerd\b|\bnee\s*,?\s+(?:bedankt|dank\s*u)\b",
    r"\bnie\s+(?:jestem|jeste[śs]my)\s+zainteresowan\w*",
    r"\bikke\s+interesser(?:et|ede|erede)\b|\bikke\s+interessert\b|\binte\s+intresserad(?:e)?\b",
    r"\bingen\s+interesse\b|\binget\s+intresse\b|\bnej\s+tak\b|\bnej\s+tack\b|\bnei\s*,?\s+takk\b",
)


# ── Opt-out requests (consumed by the unsubscribe scanner) ──────────────────

# Unlike every list above, these do not steer the sentiment classifier: the
# unsubscribe scanner (``imap_unsubscribe``) merges them into its keyword
# pattern, and a match there unenrols the sender from every list and — when
# enabled — blocklists them. A "not interested" is a refusal, not a request
# to stop mail, so only an *instruction aimed at the sender's own address*
# counts: "remove us", "ne nous contactez plus", "Löschen Sie mich", or an
# explicit "no more mail". A bare "abmelden" / "désinscrire" / "descadastrar"
# is not enough — those words sit in the footers of replies and
# auto-responders, which is also why the scanner ignores automated mail.
OPT_OUT_PATTERNS = _compile(
    # German
    r"\b(?:entfernen|streichen|l[öo]e?schen|austragen|abmelden)\s+sie\s+(?:mich|uns)\b",
    r"\bbitte\s+(?:mich|uns)\s+(?:\w+\s+){0,6}?(?:entfernen|streichen|l[öo]e?schen|austragen|abmelden)\b",
    r"\bnehmen\s+sie\s+(?:mich|uns)\s+(?:bitte\s+)?aus\b",
    r"\b(?:senden|schicken|schreiben)\s+sie\s+(?:mir|uns)\s+(?:bitte\s+)?"
    r"(?:k[üu]nftig\s+|zuk[üu]nftig\s+|in\s+zukunft\s+)?keine\b",
    r"\bkontaktier\w*\s+sie\s+(?:mich|uns)\s+(?:bitte\s+)?(?:nicht|nie)\b",
    r"\b(?:uns|mich)\s+(?:bitte\s+)?nicht\s+mehr\s+(?:kontaktieren|anschreiben|mailen)\b",
    r"\bkeine\s+(?:weiteren?\s+)?(?:e-?mails?|mails?|werbung|nachrichten|newsletter)\s+mehr\b",
    # French
    r"\b(?:d[ée]sinscri\w+|d[ée]sabonn\w+)(?:-|\s+)(?:moi|nous)\b",
    r"\b(?:retir|supprim|enlev|radi)\w+-(?:moi|nous)\b",
    r"\b(?:me|nous)\s+(?:d[ée]sinscrire|d[ée]sabonner)\b",
    r"\b(?:me|nous)\s+(?:retirer|supprimer|enlever|radier)\b"
    r"(?=[^.\n]{0,40}\b(?:liste|mailing|diffusion|base|fichier|contacts?|envois?|newsletter)\b)",
    r"\bne\s+(?:(?:me|nous)\s+)?(?:contactez|envoyez|[ée]crivez)\s+(?:(?:me|nous)\s+)?plus\b",
    r"\bne\s+plus\s+(?:nous\s+|me\s+)?(?:contacter|envoyer|[ée]crire)\b",
    # Spanish
    r"\bdar(?:me|nos)\s+de\s+baja\b|\bd[ée]n?(?:me|nos)\s+de\s+baja\b",
    r"\bde\s+baja\s+(?:de|en)\s+(?:su|sus|vuestra|vuestras|la|esta|este|ese)\s+"
    r"(?:lista|listas|base|newsletter|bolet[ií]n|correos|boletines)\b",
    r"\b(?:elim[ií]n|b[óo]rr|qu[ií]t|ret[ií]r)\w*(?:me|nos)\b",
    r"\bno\s+(?:me|nos)\s+(?:escriban?|env[ií]en?|contacten?|manden?|molesten?)\b",
    r"\bdej(?:e|en|a|ad)\s+de\s+(?:escribir|enviar|contactar|mandar|molestar)(?:me|nos)\b",
    # Italian
    r"\b(?:rimuov|elimin|disiscriv)\w*(?:mi|ci)\b",
    r"\bcancell\w*(?:mi|ci)\b(?=\s+(?:\w+\s+){0,2}d(?:a|al|alla|alle|ai)\b)",
    r"\btoglie?te(?:mi|ci)\s+d(?:a|al|alla|alle|ai)\b",
    r"\bnon\s+(?:ci|mi)\s+(?:contattate|scrivete|inviate|mandate|disturbate)\b",
    r"\bnon\s+(?:contattateci|scriveteci|inviateci|mandateci|disturbateci|contattatemi|scrivetemi)\b",
    # Portuguese
    r"\b(?:remov|retir|exclu|cancel|apag)\w*-(?:me|nos)\b|\bdescadastr\w*-(?:me|nos)\b",
    r"\bn[ãa]o\s+(?:me|nos)\s+(?:envie|enviem|contacte|contactem|escreva|escrevam|incomode|incomodem)\b",
    # Dutch
    r"\bverwijder\s+(?:me|mij|ons)\b",
    r"\bschrijf\s+(?:me|mij|ons)\s+(?:alstublieft\s+|a\.?u\.?b\.?\s+)?uit\b",
    r"\bmeld\s+(?:me|mij|ons)\s+(?:alstublieft\s+)?af\b",
    r"\bgeen\s+(?:e-?mails?|mails?|berichten|reclame)\s+meer\b",
    # Polish
    r"\b(?:usu[nń]|wypisz|wykre[sś]l|skasuj)\w*\s+(?:mnie|nas)\b",
    r"\b(?:mnie|nas)\s+(?:\w+\s+){0,3}(?:usu[nń]|wypis|wykre[sś]l)\w*",
    r"\bnie\s+(?:pisa[ćc]|wysy[łl]a[ćc]|kontaktowa[ćc])\s+(?:wi[ęe]cej|do\s+(?:nas|mnie)|nam|mi)\b",
    r"\bnie\s+(?:piszcie|wysy[łl]ajcie|kontaktujcie)\b",
    # Danish / Norwegian / Swedish
    r"\b(?:fjern|slet|slett)\s+(?:mig|os|meg|oss)\b",
    r"\b(?:afmeld|meld)\s+(?:mig|os|meg|oss)\s+(?:af|av)\b",
    r"\bikke\s+(?:flere|mer|mere)\s+(?:e-?mails?|mails?|henvendelser)\b",
    r"\bta\s+bort\s+(?:mig|oss)\b|\bta\s+(?:meg|oss)\s+av\s+(?:listen|e-?postlisten)\b",
    r"\b(?:avregistrera|avsluta)\s+(?:mig|oss)\b",
    r"\b(?:skicka|mejla|maila)\s+inte\s+(?:fler|mer)\b",
)
