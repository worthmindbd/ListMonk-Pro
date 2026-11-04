"""
Static reference data for the email validator.

Kept apart from the logic so the lists can grow without touching the checks.
Everything here is lowercase. The lists are deliberately *conservative*: a
domain or prefix only belongs in them when flagging it is almost always right,
because every flag shows up in the Email Validation page.

Nothing in this module is ever used to blocklist anybody on its own:
disposable domains, role accounts and typos produce the ``risky`` verdict,
which the scheduled cleanup never acts on (see ``email_cleanup``).
"""

# Local parts (the text before the @) that normally belong to a team or a
# system rather than a person. Taken from the repo's cleanup_generic_emails.py
# script (which cannot be imported: it loads .env and talks to ListMonk at
# import) plus the most common greeting-style aliases.
ROLE_LOCAL_PARTS: frozenset[str] = frozenset({
    "abuse", "accounts", "admin", "administrator", "billing", "careers",
    "ceo", "cfo", "coo", "cto", "compliance", "complaints", "contact",
    "data", "director", "enquiries", "enquiry", "feedback", "finance",
    "general", "hello", "help", "helpdesk", "hostmaster", "hr", "info",
    "inquiries", "inquiry", "jobs", "legal", "mail", "mailer-daemon",
    "management", "manager", "marketing", "media", "news", "newsletter",
    "no-reply", "noreply", "do-not-reply", "donotreply", "office",
    "operations", "orders", "postmaster", "press", "privacy", "recruitment",
    "sales", "security", "service", "staff", "support", "team", "ticket",
    "webmaster",
})

# Throwaway-inbox providers. Not exhaustive (new ones appear daily); the list
# targets the services that dominate real-world sign-up abuse.
DISPOSABLE_DOMAINS: frozenset[str] = frozenset({
    "0-mail.com", "10minutemail.com", "10minutemail.net", "10minutemail.org",
    "20minutemail.com", "33mail.com", "anonbox.net", "armyspy.com",
    "binkmail.com", "bobmail.info", "burnermail.io", "byom.de",
    "chammy.info", "cool.fr.nf", "cuvox.de", "dayrep.com", "deadaddress.com",
    "discard.email", "discardmail.com", "discardmail.de",     "disposableemailaddresses.com", "dispostable.com", "dodgeit.com",
    "dropmail.me", "dump-email.info", "dumpmail.de", "einrot.com",
    "emailondeck.com", "emailfake.com", "emailtemporanea.com",
    "emailtemporanea.net", "fakeinbox.com", "fakemail.fr", "fakemailgenerator.com",
    "fleckens.hu", "getairmail.com", "getnada.com", "gishpuppy.com",
    "gustr.com", "guerrillamail.biz", "guerrillamail.com",
    "guerrillamail.de", "guerrillamail.info", "guerrillamail.net",
    "guerrillamail.org", "guerrillamailblock.com", "harakirimail.com",
    "hidemail.de", "imgof.com", "incognitomail.com", "incognitomail.net",
    "incognitomail.org", "inboxalias.com", "inboxbear.com", "instantemailaddress.com",
    "jetable.com", "jetable.fr.nf", "jetable.net", "jetable.org",
    "jourrapide.com", "kasmail.com", "klzlk.com", "koszmail.pl",
    "kurzepost.de", "letthemeatspam.com", "lroid.com", "mail-temporaire.fr",
    "mail.tm", "mail7.io", "mailcatch.com", "maildrop.cc", "maildrop.cf",
    "maildrop.ga", "maildrop.gq", "maildrop.ml", "maildu.de",
    "maileater.com", "mailexpire.com", "mailforspam.com", "mailin8r.com",
    "mailinater.com", "mailinator.com", "mailinator.net", "mailinator.org",
    "mailinator2.com", "mailmetrash.com", "mailmoat.com", "mailnesia.com",
    "mailnull.com", "mailsac.com", "mailscrap.com", "mailseal.de",
    "mailtemp.info", "mailtothis.com", "mailzilla.com",
    "meltmail.com", "mintemail.com", "moakt.com", "mohmal.com",
    "mt2014.com", "mt2015.com", "mytemp.email", "mytempemail.com",
    "mytrashmail.com", "nada.email", "nepwk.com", "no-spam.ws", "nobulk.com",
    "noclickemail.com", "nomail.xl.cx", "nospam.ze.tc", "nospamfor.us",
    "nowmymail.com", "objectmail.com", "obobbo.com", "oneoffemail.com",
    "pjjkp.com", "plexolan.de", "pookmail.com",     "proxymail.eu", "putthisinyouremail.com", "quickinbox.com",
    "rcpt.at", "reallymymail.com", "recode.me", "regbypass.com",
    "rhyta.com", "rmqkr.net", "safetymail.info", "sharklasers.com",
    "shieldedmail.com", "shitmail.me", "skeefmail.com", "slopsbox.com",
    "smellfear.com", "snakemail.com", "sneakemail.com", "sofort-mail.de",
    "spam4.me", "spamavert.com", "spambob.com", "spambob.net", "spambob.org",
    "spambog.com", "spambog.de", "spambog.ru", "spamcero.com", "spamday.com",
    "spamex.com", "spamfree24.com", "spamfree24.de", "spamfree24.eu",
    "spamfree24.info", "spamfree24.net", "spamfree24.org", "spamgourmet.com",
    "spamherelots.com", "spamhereplease.com", "spamhole.com", "spamify.com",
    "spaminator.de", "spamkill.info", "spaml.com", "spaml.de", "spammotel.com",
    "spamobox.com", "spamoff.de", "spamslicer.com", "spamspot.com",
    "spamthis.co.uk", "spamthisplease.com", "spamtrail.com",     "superrito.com", "suremail.info", "teleworm.com", "teleworm.us",
    "tempail.com", "tempalias.com", "tempe-mail.com", "tempemail.biz",
    "tempemail.co.za", "tempemail.com", "tempemail.net", "tempinbox.co.uk",
    "tempinbox.com", "tempmail.de", "tempmail.eu", "tempmail.it",
    "tempmail.net", "tempmail.org", "tempmail.us", "tempmaildemo.com",
    "tempmailer.com", "tempmailer.de", "tempomail.fr", "temporarily.de",
    "temporaryemail.net", "temporaryemail.us", "temporaryforwarding.com",
    "temporaryinbox.com", "temporarymailaddress.com", "thankyou2010.com",
    "thc.st", "thetrash.email", "throam.com", "throwam.com",
    "throwawayemailaddress.com", "throwawaymail.com", "tmail.ws", "tmailinator.com",
    "trash-mail.at", "trash-mail.com", "trash-mail.de", "trash2009.com",
    "trashdevil.com", "trashemail.de", "trashmail.at", "trashmail.com",
    "trashmail.de", "trashmail.io", "trashmail.me", "trashmail.net",
    "trashmail.org", "trashmail.ws", "trashmailer.com", "trashymail.com",
    "trbvm.com", "turual.com", "twinmail.de", "tyldd.com", "uggsrock.com",
    "upliftnow.com", "uplipht.com", "venompen.com", "veryrealemail.com",
    "viditag.com", "viewcastmedia.com", "viewcastmedia.net", "viewcastmedia.org",
    "webm4il.info", "wegwerfadresse.de", "wegwerfemail.com", "wegwerfemail.de",
    "wegwerfmail.de", "wegwerfmail.info", "wegwerfmail.net", "wegwerfmail.org",
    "wh4f.org", "whyspam.me", "willhackforfood.biz", "willselfdestruct.com",
    "winemaven.info", "wronghead.com", "wuzup.net", "wuzupmail.net",
    "yepmail.net", "yopmail.com", "yopmail.fr", "yopmail.net",     "yuurok.com", "zehnminutenmail.de", "zippymail.info", "zoemail.net",
    "zoemail.org",
})

# The mailbox providers people actually mistype. Used two ways: an exact
# misspelling table (below) and an edit-distance-1 match against this set.
TOP_PROVIDER_DOMAINS: tuple[str, ...] = (
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "aol.com", "live.com", "msn.com", "proton.me", "protonmail.com",
    "mail.com", "gmx.com", "gmx.de", "yandex.com", "zoho.com",
    "comcast.net", "me.com", "yahoo.co.uk", "hotmail.co.uk", "googlemail.com",
)

# Misspellings that are unambiguous. Maps typo -> intended domain.
TYPO_DOMAINS: dict[str, str] = {
    "gmial.com": "gmail.com", "gmai.com": "gmail.com", "gmail.co": "gmail.com",
    "gmail.con": "gmail.com", "gmail.cm": "gmail.com", "gmail.om": "gmail.com",
    "gmal.com": "gmail.com", "gmil.com": "gmail.com", "gmaill.com": "gmail.com",
    "gamil.com": "gmail.com", "gnail.com": "gmail.com", "gmeil.com": "gmail.com",
    "gmail.comm": "gmail.com", "gmailc.om": "gmail.com", "ggmail.com": "gmail.com",
    "gimail.com": "gmail.com", "gmaik.com": "gmail.com", "gmali.com": "gmail.com",
    "yaho.com": "yahoo.com", "yahooo.com": "yahoo.com", "yahoo.co": "yahoo.com",
    "yahoo.con": "yahoo.com", "yhoo.com": "yahoo.com", "yaoo.com": "yahoo.com",
    "yahho.com": "yahoo.com", "yahou.com": "yahoo.com", "ymail.con": "ymail.com",
    "hotmal.com": "hotmail.com", "hotmai.com": "hotmail.com",
    "hotmail.co": "hotmail.com", "hotmail.con": "hotmail.com",
    "hotmial.com": "hotmail.com", "homail.com": "hotmail.com",
    "hotmil.com": "hotmail.com", "hitmail.com": "hotmail.com",
    "hotmaill.com": "hotmail.com", "hptmail.com": "hotmail.com",
    "outlok.com": "outlook.com", "outloook.com": "outlook.com",
    "outlook.co": "outlook.com", "outlook.con": "outlook.com",
    "outook.com": "outlook.com", "outllook.com": "outlook.com",
    "iclod.com": "icloud.com", "icloud.co": "icloud.com",
    "icloud.con": "icloud.com", "icoud.com": "icloud.com",
    "iclould.com": "icloud.com",
    "aol.co": "aol.com", "aol.con": "aol.com",
    "protonmail.co": "protonmail.com", "protonmail.con": "protonmail.com",
    "live.co": "live.com", "live.con": "live.com",
    "msn.co": "msn.com", "msn.con": "msn.com",
}

# Top-level domains that are a one-letter slip from .com / .net / .org and are
# not real registrations in practice. Only applied to *suggest* a fix.
TYPO_TLDS: dict[str, str] = {
    "con": "com", "cmo": "com", "vom": "com", "xom": "com", "ocm": "com",
    "comm": "com", "cim": "com", "coom": "com",
    "nett": "net", "ent": "net", "met": "net",
    "ogr": "org", "orgg": "org",
}
