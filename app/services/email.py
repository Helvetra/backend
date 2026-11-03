"""
Email service for sending transactional emails.
Handles SMTP connection and multilingual email template rendering.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Brand colors matching frontend tailwind.config.ts
SWISS_RED = "#DA291C"
SWISS_RED_LIGHT = "#E84C41"  # Lighter red for dark mode visibility
NEUTRAL_700 = "#404040"
NEUTRAL_500 = "#737373"
NEUTRAL_200 = "#E5E5E5"
NEUTRAL_100 = "#F5F5F5"

# Dark mode colors
DARK_BG = "#1a1a1a"
DARK_TEXT = "#E5E5E5"
DARK_TEXT_MUTED = "#A3A3A3"

# Supported locales (excluding gsw - uses de for emails)
SUPPORTED_LOCALES = ("en", "de", "fr", "it")
DEFAULT_LOCALE = "en"

# Email translations
TRANSLATIONS = {
    "verification": {
        "en": {
            "subject": "Verify your Helvetra account",
            "welcome": "Welcome to Helvetra!",
            "body": "Please verify your email address by clicking the button below:",
            "button": "Verify Email",
            "link_text": "Or copy and paste this link into your browser:",
            "expires": "This link expires in {hours} hours.",
            "ignore": "If you didn't create a Helvetra account, you can safely ignore this email.",
        },
        "de": {
            "subject": "Bestätigen Sie Ihr Helvetra-Konto",
            "welcome": "Willkommen bei Helvetra!",
            "body": "Bitte bestätigen Sie Ihre E-Mail-Adresse, indem Sie auf die Schaltfläche unten klicken:",
            "button": "E-Mail bestätigen",
            "link_text": "Oder kopieren Sie diesen Link in Ihren Browser:",
            "expires": "Dieser Link läuft in {hours} Stunden ab.",
            "ignore": "Wenn Sie kein Helvetra-Konto erstellt haben, können Sie diese E-Mail ignorieren.",
        },
        "fr": {
            "subject": "Vérifiez votre compte Helvetra",
            "welcome": "Bienvenue sur Helvetra !",
            "body": "Veuillez vérifier votre adresse e-mail en cliquant sur le bouton ci-dessous :",
            "button": "Vérifier l'e-mail",
            "link_text": "Ou copiez et collez ce lien dans votre navigateur :",
            "expires": "Ce lien expire dans {hours} heures.",
            "ignore": "Si vous n'avez pas créé de compte Helvetra, vous pouvez ignorer cet e-mail.",
        },
        "it": {
            "subject": "Verifica il tuo account Helvetra",
            "welcome": "Benvenuto su Helvetra!",
            "body": "Verifica il tuo indirizzo e-mail cliccando sul pulsante qui sotto:",
            "button": "Verifica e-mail",
            "link_text": "Oppure copia e incolla questo link nel tuo browser:",
            "expires": "Questo link scade tra {hours} ore.",
            "ignore": "Se non hai creato un account Helvetra, puoi ignorare questa e-mail.",
        },
    },
    "b2b_trial_ending": {
        "en": {
            "subject": "Your Helvetra API trial ends soon",
            "intro": "Hi,",
            "body": (
                "Your Helvetra API trial ends in 3 days. After that, your "
                "card will be charged for the first month (CHF 39). Keep "
                "using the API as normal, there is nothing to do."
            ),
            "button": "Open your dashboard",
            "link_text": "Or open this link in your browser:",
            "manage": (
                "Want to stop the trial? You can cancel from the developer "
                "dashboard under \"Manage billing\"."
            ),
            "ignore": "Questions? Just reply to this email.",
        },
        "de": {
            "subject": "Ihre Helvetra-API-Testphase endet bald",
            "intro": "Hallo,",
            "body": (
                "Ihre Helvetra-API-Testphase endet in 3 Tagen. Danach wird "
                "Ihre Karte für den ersten Monat (CHF 39) belastet. Nutzen "
                "Sie die API einfach weiter, es ist nichts zu tun."
            ),
            "button": "Zum Dashboard",
            "link_text": "Oder öffnen Sie diesen Link im Browser:",
            "manage": (
                "Möchten Sie die Testphase beenden? Das geht im "
                "Entwickler-Dashboard unter \"Abrechnung verwalten\"."
            ),
            "ignore": "Fragen? Antworten Sie einfach auf diese E-Mail.",
        },
        "fr": {
            "subject": "Votre essai de l'API Helvetra se termine bientôt",
            "intro": "Bonjour,",
            "body": (
                "Votre essai de l'API Helvetra se termine dans 3 jours. "
                "Votre carte sera ensuite débitée du premier mois (CHF 39). "
                "Continuez à utiliser l'API normalement, il n'y a rien à "
                "faire."
            ),
            "button": "Ouvrir le tableau de bord",
            "link_text": "Ou ouvrez ce lien dans votre navigateur :",
            "manage": (
                "Vous voulez arrêter l'essai ? Vous pouvez annuler depuis "
                "le tableau de bord développeur, sous « Gérer la "
                "facturation »."
            ),
            "ignore": "Des questions ? Répondez simplement à cet e-mail.",
        },
        "it": {
            "subject": "La tua prova dell'API Helvetra termina presto",
            "intro": "Ciao,",
            "body": (
                "La tua prova dell'API Helvetra termina tra 3 giorni. Dopo, "
                "la tua carta sarà addebitata per il primo mese (CHF 39). "
                "Continua a usare l'API normalmente, non c'è nulla da fare."
            ),
            "button": "Apri la dashboard",
            "link_text": "Oppure apri questo link nel browser:",
            "manage": (
                "Vuoi interrompere la prova? Puoi annullarla dalla "
                "dashboard sviluppatori sotto \"Gestisci fatturazione\"."
            ),
            "ignore": "Domande? Rispondi semplicemente a questa e-mail.",
        },
    },
    "b2b_usage_80": {
        "en": {
            "subject": "You've used 80% of your Helvetra API quota",
            "intro": "Hi,",
            "body": (
                "Heads up: you've used 80% of your included characters for "
                "this billing period. Nothing changes yet, this is just so "
                "you can plan ahead."
            ),
            "button": "Check usage in the dashboard",
            "link_text": "Or open this link in your browser:",
            "manage": (
                "Need more capacity? Reply to this email and we'll set up "
                "an Enterprise plan for you."
            ),
            "ignore": "Questions? Just reply to this email.",
        },
        "de": {
            "subject": "Sie haben 80% Ihres Helvetra-API-Kontingents genutzt",
            "intro": "Hallo,",
            "body": (
                "Kurzer Hinweis: Sie haben 80% Ihrer inklusive Zeichen in "
                "dieser Abrechnungsperiode genutzt. Es ändert sich noch "
                "nichts, das ist nur zur Planung."
            ),
            "button": "Verbrauch im Dashboard ansehen",
            "link_text": "Oder öffnen Sie diesen Link im Browser:",
            "manage": (
                "Brauchen Sie mehr Kapazität? Antworten Sie auf diese "
                "E-Mail, dann richten wir Ihnen einen Enterprise-Tarif ein."
            ),
            "ignore": "Fragen? Antworten Sie einfach auf diese E-Mail.",
        },
        "fr": {
            "subject": "Vous avez utilisé 80 % de votre quota d'API Helvetra",
            "intro": "Bonjour,",
            "body": (
                "Petit message : vous avez utilisé 80 % des caractères "
                "inclus pour cette période de facturation. Rien ne change "
                "pour l'instant, c'est juste pour anticiper."
            ),
            "button": "Voir l'utilisation dans le tableau de bord",
            "link_text": "Ou ouvrez ce lien dans votre navigateur :",
            "manage": (
                "Besoin de plus de capacité ? Répondez à cet e-mail et nous "
                "mettrons en place une offre Enterprise pour vous."
            ),
            "ignore": "Des questions ? Répondez simplement à cet e-mail.",
        },
        "it": {
            "subject": "Hai usato l'80% del tuo quota API Helvetra",
            "intro": "Ciao,",
            "body": (
                "Un avviso: hai usato l'80% dei caratteri inclusi per "
                "questo periodo di fatturazione. Per ora non cambia nulla, "
                "è solo per pianificare."
            ),
            "button": "Vedi l'utilizzo nella dashboard",
            "link_text": "Oppure apri questo link nel browser:",
            "manage": (
                "Hai bisogno di più capacità? Rispondi a questa e-mail e "
                "attiveremo per te un piano Enterprise."
            ),
            "ignore": "Domande? Rispondi semplicemente a questa e-mail.",
        },
    },
    "b2b_usage_100": {
        "en": {
            "subject": "You've reached your Helvetra API quota",
            "intro": "Hi,",
            "body": (
                "You've used 100% of the characters included in your "
                "current billing period. Further translation requests will "
                "be rejected until the next period starts."
            ),
            "button": "Manage your plan",
            "link_text": "Or open this link in your browser:",
            "manage": (
                "Need more capacity? Reply to this email and we'll set up "
                "an Enterprise plan for you."
            ),
            "ignore": "Questions? Just reply to this email.",
        },
        "de": {
            "subject": "Sie haben Ihr Helvetra-API-Kontingent erreicht",
            "intro": "Hallo,",
            "body": (
                "Sie haben 100% der inklusive Zeichen in Ihrer aktuellen "
                "Abrechnungsperiode genutzt. Weitere Übersetzungs-Anfragen "
                "werden bis zum Beginn der nächsten Periode abgelehnt."
            ),
            "button": "Tarif verwalten",
            "link_text": "Oder öffnen Sie diesen Link im Browser:",
            "manage": (
                "Brauchen Sie mehr Kapazität? Antworten Sie auf diese "
                "E-Mail, dann richten wir Ihnen einen Enterprise-Tarif ein."
            ),
            "ignore": "Fragen? Antworten Sie einfach auf diese E-Mail.",
        },
        "fr": {
            "subject": "Vous avez atteint votre quota d'API Helvetra",
            "intro": "Bonjour,",
            "body": (
                "Vous avez utilisé 100 % des caractères inclus dans votre "
                "période de facturation actuelle. Les requêtes "
                "supplémentaires seront rejetées jusqu'au début de la "
                "prochaine période."
            ),
            "button": "Gérer votre offre",
            "link_text": "Ou ouvrez ce lien dans votre navigateur :",
            "manage": (
                "Besoin de plus de capacité ? Répondez à cet e-mail et nous "
                "mettrons en place une offre Enterprise pour vous."
            ),
            "ignore": "Des questions ? Répondez simplement à cet e-mail.",
        },
        "it": {
            "subject": "Hai raggiunto il tuo quota API Helvetra",
            "intro": "Ciao,",
            "body": (
                "Hai usato il 100% dei caratteri inclusi nel periodo di "
                "fatturazione corrente. Le ulteriori richieste di "
                "traduzione saranno rifiutate fino all'inizio del prossimo "
                "periodo."
            ),
            "button": "Gestisci il piano",
            "link_text": "Oppure apri questo link nel browser:",
            "manage": (
                "Hai bisogno di più capacità? Rispondi a questa e-mail e "
                "attiveremo per te un piano Enterprise."
            ),
            "ignore": "Domande? Rispondi semplicemente a questa e-mail.",
        },
    },
    "password_reset": {
        "en": {
            "subject": "Reset your Helvetra password",
            "intro": "You requested a password reset for your Helvetra account.",
            "body": "Click the button below to set a new password:",
            "button": "Reset Password",
            "link_text": "Or copy and paste this link into your browser:",
            "expires": "This link expires in 1 hour.",
            "ignore": "If you didn't request a password reset, you can safely ignore this email.",
        },
        "de": {
            "subject": "Setzen Sie Ihr Helvetra-Passwort zurück",
            "intro": "Sie haben eine Passwortzurücksetzung für Ihr Helvetra-Konto angefordert.",
            "body": "Klicken Sie auf die Schaltfläche unten, um ein neues Passwort festzulegen:",
            "button": "Passwort zurücksetzen",
            "link_text": "Oder kopieren Sie diesen Link in Ihren Browser:",
            "expires": "Dieser Link läuft in 1 Stunde ab.",
            "ignore": "Wenn Sie keine Passwortzurücksetzung angefordert haben, können Sie diese E-Mail ignorieren.",
        },
        "fr": {
            "subject": "Réinitialisez votre mot de passe Helvetra",
            "intro": "Vous avez demandé une réinitialisation de mot de passe pour votre compte Helvetra.",
            "body": "Cliquez sur le bouton ci-dessous pour définir un nouveau mot de passe :",
            "button": "Réinitialiser le mot de passe",
            "link_text": "Ou copiez et collez ce lien dans votre navigateur :",
            "expires": "Ce lien expire dans 1 heure.",
            "ignore": "Si vous n'avez pas demandé de réinitialisation de mot de passe, vous pouvez ignorer cet e-mail.",
        },
        "it": {
            "subject": "Reimposta la tua password Helvetra",
            "intro": "Hai richiesto la reimpostazione della password per il tuo account Helvetra.",
            "body": "Clicca sul pulsante qui sotto per impostare una nuova password:",
            "button": "Reimposta password",
            "link_text": "Oppure copia e incolla questo link nel tuo browser:",
            "expires": "Questo link scade tra 1 ora.",
            "ignore": "Se non hai richiesto la reimpostazione della password, puoi ignorare questa e-mail.",
        },
    },
}


def get_locale(locale: str | None) -> str:
    """Normalize locale to supported value. Maps gsw to de."""
    if not locale:
        return DEFAULT_LOCALE
    # Swiss German uses German for emails
    if locale == "gsw":
        return "de"
    if locale in SUPPORTED_LOCALES:
        return locale
    return DEFAULT_LOCALE


def get_translation(email_type: str, locale: str | None) -> dict[str, str]:
    """Get translations for an email type and locale."""
    normalized_locale = get_locale(locale)
    return TRANSLATIONS[email_type][normalized_locale]


class EmailService:
    """Send transactional emails via SMTP."""

    def _create_smtp_connection(self) -> smtplib.SMTP:
        """Create and authenticate SMTP connection."""
        smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port)
        smtp.starttls()
        smtp.login(settings.smtp_user, settings.smtp_password)
        return smtp

    def _build_message(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: str | None = None,
    ) -> MIMEMultipart:
        """Build a multipart email message."""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{settings.smtp_from_name} <{settings.smtp_from_email}>"
        msg["To"] = to_email

        # Plain text fallback
        if text_content:
            msg.attach(MIMEText(text_content, "plain", "utf-8"))

        # HTML content
        msg.attach(MIMEText(html_content, "html", "utf-8"))

        return msg

    def send_email(
        self,
        to_email: str,
        subject: str,
        html_content: str,
        text_content: str | None = None,
    ) -> bool:
        """Send an email. Returns True on success, False on failure."""
        if not settings.smtp_user or not settings.smtp_password:
            logger.warning("SMTP credentials not configured, skipping email send")
            return False

        try:
            msg = self._build_message(to_email, subject, html_content, text_content)

            with self._create_smtp_connection() as smtp:
                smtp.sendmail(
                    settings.smtp_from_email,
                    to_email,
                    msg.as_string(),
                )

            logger.info(f"Email sent successfully to {to_email}")
            return True

        except smtplib.SMTPAuthenticationError:
            logger.error("SMTP authentication failed")
            return False
        except smtplib.SMTPException as e:
            logger.error(f"SMTP error sending email: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error sending email: {e}")
            return False

    def _build_html_template(
        self,
        welcome_or_intro: str,
        body: str,
        button_text: str,
        button_url: str,
        link_text: str,
        expires: str,
        ignore: str,
    ) -> str:
        """Build the standard HTML email template with dark mode support."""
        return f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta name="color-scheme" content="light dark">
    <meta name="supported-color-schemes" content="light dark">
    <style>
        :root {{
            color-scheme: light dark;
            supported-color-schemes: light dark;
        }}
        @media (prefers-color-scheme: dark) {{
            .email-body {{
                background-color: {DARK_BG} !important;
                color: {DARK_TEXT} !important;
            }}
            .email-text {{
                color: {DARK_TEXT} !important;
            }}
            .email-muted {{
                color: {DARK_TEXT_MUTED} !important;
            }}
            .email-link {{
                color: {SWISS_RED_LIGHT} !important;
            }}
            .email-button {{
                background-color: {SWISS_RED_LIGHT} !important;
            }}
            .email-divider {{
                border-top-color: #404040 !important;
            }}
            .email-logo {{
                filter: brightness(0) invert(1);
            }}
        }}
    </style>
</head>
<body class="email-body" style="font-family: 'Helvetica Neue', Helvetica, Arial, sans-serif; line-height: 1.6; color: {NEUTRAL_700}; background-color: #ffffff; max-width: 600px; margin: 0 auto; padding: 20px; -webkit-font-smoothing: antialiased;">
    <div style="text-align: center; margin-bottom: 30px;">
        <img class="email-logo" src="https://helvetra.ch/img/logo.png" alt="Helvetra" width="130" height="32" style="display: block; margin: 0 auto;">
    </div>

    <p class="email-text">{welcome_or_intro}</p>

    <p class="email-text">{body}</p>

    <div style="text-align: center; margin: 30px 0;">
        <a href="{button_url}" class="email-button"
           style="background-color: {SWISS_RED}; color: white; padding: 12px 30px; text-decoration: none; border-radius: 6px; display: inline-block; font-weight: 500;">
            {button_text}
        </a>
    </div>

    <p class="email-muted" style="color: {NEUTRAL_500}; font-size: 14px;">
        {link_text}<br>
        <a href="{button_url}" class="email-link" style="color: {SWISS_RED};">{button_url}</a>
    </p>

    <p class="email-muted" style="color: {NEUTRAL_500}; font-size: 14px;">
        {expires}
    </p>

    <hr class="email-divider" style="border: none; border-top: 1px solid {NEUTRAL_200}; margin: 30px 0;">

    <p class="email-muted" style="color: {NEUTRAL_500}; font-size: 12px;">
        {ignore}
    </p>
</body>
</html>
"""

    def _build_text_template(
        self,
        welcome_or_intro: str,
        body: str,
        button_url: str,
        expires: str,
        ignore: str,
    ) -> str:
        """Build the plain text email template."""
        return f"""
{welcome_or_intro}

{body}
{button_url}

{expires}

{ignore}
"""

    def send_verification_email(
        self, to_email: str, token: str, locale: str | None = None
    ) -> bool:
        """Send email verification link to user."""
        verification_url = f"{settings.email_verification_base_url}?token={token}"
        t = get_translation("verification", locale)

        html_content = self._build_html_template(
            welcome_or_intro=t["welcome"],
            body=t["body"],
            button_text=t["button"],
            button_url=verification_url,
            link_text=t["link_text"],
            expires=t["expires"].format(hours=settings.email_verification_expire_hours),
            ignore=t["ignore"],
        )

        text_content = self._build_text_template(
            welcome_or_intro=t["welcome"],
            body=t["body"],
            button_url=verification_url,
            expires=t["expires"].format(hours=settings.email_verification_expire_hours),
            ignore=t["ignore"],
        )

        return self.send_email(to_email, t["subject"], html_content, text_content)

    def send_b2b_trial_ending_email(
        self, to_email: str, locale: str | None = None
    ) -> bool:
        """
        Notify a B2B customer that their 14-day API trial ends in
        ~3 days, so they can upgrade, cancel, or do nothing as they prefer.
        Triggered by the Stripe customer.subscription.trial_will_end
        webhook event.
        """
        dashboard_url = "https://helvetra.ch/developers/dashboard"
        t = get_translation("b2b_trial_ending", locale)

        html_content = self._build_html_template(
            welcome_or_intro=t["intro"],
            body=t["body"],
            button_text=t["button"],
            button_url=dashboard_url,
            link_text=t["link_text"],
            expires=t["manage"],
            ignore=t["ignore"],
        )

        text_content = self._build_text_template(
            welcome_or_intro=t["intro"],
            body=t["body"],
            button_url=dashboard_url,
            expires=t["manage"],
            ignore=t["ignore"],
        )

        return self.send_email(to_email, t["subject"], html_content, text_content)

    def send_b2b_usage_alert_email(
        self,
        to_email: str,
        threshold: int,
        locale: str | None = None,
    ) -> bool:
        """
        Notify a B2B customer that their character usage has crossed a
        threshold (80 or 100 percent) of the included quota for the
        current billing period. Fires at most once per threshold per
        period thanks to the alert_*_sent flags on usage_periods.
        """
        if threshold not in (80, 100):
            raise ValueError(f"Unsupported usage-alert threshold: {threshold}")

        dashboard_url = "https://helvetra.ch/developers/dashboard"
        t = get_translation(f"b2b_usage_{threshold}", locale)

        html_content = self._build_html_template(
            welcome_or_intro=t["intro"],
            body=t["body"],
            button_text=t["button"],
            button_url=dashboard_url,
            link_text=t["link_text"],
            expires=t["manage"],
            ignore=t["ignore"],
        )

        text_content = self._build_text_template(
            welcome_or_intro=t["intro"],
            body=t["body"],
            button_url=dashboard_url,
            expires=t["manage"],
            ignore=t["ignore"],
        )

        return self.send_email(to_email, t["subject"], html_content, text_content)

    def send_password_reset_email(
        self, to_email: str, token: str, locale: str | None = None
    ) -> bool:
        """Send password reset link to user."""
        reset_url = f"https://helvetra.ch/reset-password?token={token}"
        t = get_translation("password_reset", locale)

        html_content = self._build_html_template(
            welcome_or_intro=t["intro"],
            body=t["body"],
            button_text=t["button"],
            button_url=reset_url,
            link_text=t["link_text"],
            expires=t["expires"],
            ignore=t["ignore"],
        )

        text_content = self._build_text_template(
            welcome_or_intro=t["intro"],
            body=t["body"],
            button_url=reset_url,
            expires=t["expires"],
            ignore=t["ignore"],
        )

        return self.send_email(to_email, t["subject"], html_content, text_content)


# Global email service instance
email_service = EmailService()
