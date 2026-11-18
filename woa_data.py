"""Lookup tables for the installer UI: locale names and Windows time zone IDs."""
import subprocess
import sys

# BCP-47 tag -> English display name (common Windows languages / regions)
LOCALES = {
    "af-ZA": "Afrikaans (South Africa)", "sq-AL": "Albanian (Albania)", "ar-SA": "Arabic (Saudi Arabia)",
    "ar-AE": "Arabic (U.A.E.)", "ar-EG": "Arabic (Egypt)", "hy-AM": "Armenian (Armenia)",
    "az-Latn-AZ": "Azerbaijani (Latin, Azerbaijan)", "eu-ES": "Basque (Spain)", "be-BY": "Belarusian (Belarus)",
    "bn-IN": "Bangla (India)", "bs-Latn-BA": "Bosnian (Latin, Bosnia and Herzegovina)", "bg-BG": "Bulgarian (Bulgaria)",
    "ca-ES": "Catalan (Spain)", "zh-CN": "Chinese (Simplified, China)", "zh-TW": "Chinese (Traditional, Taiwan)",
    "zh-HK": "Chinese (Traditional, Hong Kong)", "hr-HR": "Croatian (Croatia)", "cs-CZ": "Czech (Czechia)",
    "da-DK": "Danish (Denmark)", "nl-NL": "Dutch (Netherlands)", "nl-BE": "Dutch (Belgium)",
    "en-US": "English (United States)", "en-GB": "English (United Kingdom)", "en-AU": "English (Australia)",
    "en-CA": "English (Canada)", "en-IN": "English (India)", "en-NZ": "English (New Zealand)",
    "en-IE": "English (Ireland)", "en-ZA": "English (South Africa)", "en-SG": "English (Singapore)",
    "et-EE": "Estonian (Estonia)", "fil-PH": "Filipino (Philippines)", "fi-FI": "Finnish (Finland)",
    "fr-FR": "French (France)", "fr-CA": "French (Canada)", "fr-BE": "French (Belgium)",
    "fr-CH": "French (Switzerland)", "gl-ES": "Galician (Spain)", "ka-GE": "Georgian (Georgia)",
    "de-DE": "German (Germany)", "de-AT": "German (Austria)", "de-CH": "German (Switzerland)",
    "el-GR": "Greek (Greece)", "he-IL": "Hebrew (Israel)", "hi-IN": "Hindi (India)", "hu-HU": "Hungarian (Hungary)",
    "is-IS": "Icelandic (Iceland)", "id-ID": "Indonesian (Indonesia)", "ga-IE": "Irish (Ireland)",
    "it-IT": "Italian (Italy)", "it-CH": "Italian (Switzerland)", "ja-JP": "Japanese (Japan)",
    "kk-KZ": "Kazakh (Kazakhstan)", "ko-KR": "Korean (Korea)", "lv-LV": "Latvian (Latvia)",
    "lt-LT": "Lithuanian (Lithuania)", "lb-LU": "Luxembourgish (Luxembourg)", "mk-MK": "Macedonian (North Macedonia)",
    "ms-MY": "Malay (Malaysia)", "mt-MT": "Maltese (Malta)", "mr-IN": "Marathi (India)",
    "nb-NO": "Norwegian Bokmål (Norway)", "fa-IR": "Persian (Iran)", "pl-PL": "Polish (Poland)",
    "pt-BR": "Portuguese (Brazil)", "pt-PT": "Portuguese (Portugal)", "ro-RO": "Romanian (Romania)",
    "ru-RU": "Russian (Russia)", "sr-Latn-RS": "Serbian (Latin, Serbia)", "sr-Cyrl-RS": "Serbian (Cyrillic, Serbia)",
    "sk-SK": "Slovak (Slovakia)", "sl-SI": "Slovenian (Slovenia)", "es-ES": "Spanish (Spain)",
    "es-MX": "Spanish (Mexico)", "es-US": "Spanish (United States)", "es-AR": "Spanish (Argentina)",
    "es-CO": "Spanish (Colombia)", "es-CL": "Spanish (Chile)", "es-419": "Spanish (Latin America)",
    "sw-KE": "Swahili (Kenya)", "sv-SE": "Swedish (Sweden)", "ta-IN": "Tamil (India)", "te-IN": "Telugu (India)",
    "th-TH": "Thai (Thailand)", "tr-TR": "Turkish (Türkiye)", "uk-UA": "Ukrainian (Ukraine)",
    "ur-PK": "Urdu (Pakistan)", "uz-Latn-UZ": "Uzbek (Latin, Uzbekistan)", "vi-VN": "Vietnamese (Vietnam)",
    "cy-GB": "Welsh (United Kingdom)",
}

# Windows time zone IDs (what unattend <TimeZone> expects), in UTC-offset order.
TIMEZONES = """Dateline Standard Time|UTC-11|Aleutian Standard Time|Hawaiian Standard Time|Marquesas Standard Time|
Alaskan Standard Time|UTC-09|Pacific Standard Time (Mexico)|UTC-08|Pacific Standard Time|US Mountain Standard Time|
Mountain Standard Time (Mexico)|Mountain Standard Time|Yukon Standard Time|Central America Standard Time|
Central Standard Time|Easter Island Standard Time|Central Standard Time (Mexico)|Canada Central Standard Time|
SA Pacific Standard Time|Eastern Standard Time (Mexico)|Eastern Standard Time|Haiti Standard Time|Cuba Standard Time|
US Eastern Standard Time|Turks And Caicos Standard Time|Paraguay Standard Time|Atlantic Standard Time|
Venezuela Standard Time|Central Brazilian Standard Time|SA Western Standard Time|Pacific SA Standard Time|
Newfoundland Standard Time|Tocantins Standard Time|E. South America Standard Time|SA Eastern Standard Time|
Argentina Standard Time|Greenland Standard Time|Montevideo Standard Time|Magallanes Standard Time|
Saint Pierre Standard Time|Bahia Standard Time|UTC-02|Mid-Atlantic Standard Time|Azores Standard Time|
Cape Verde Standard Time|UTC|GMT Standard Time|Greenwich Standard Time|Sao Tome Standard Time|Morocco Standard Time|
W. Europe Standard Time|Central Europe Standard Time|Romance Standard Time|Central European Standard Time|
W. Central Africa Standard Time|GTB Standard Time|Middle East Standard Time|Egypt Standard Time|E. Europe Standard Time|
West Bank Standard Time|South Africa Standard Time|FLE Standard Time|Israel Standard Time|South Sudan Standard Time|
Kaliningrad Standard Time|Sudan Standard Time|Libya Standard Time|Namibia Standard Time|Jordan Standard Time|
Arabic Standard Time|Syria Standard Time|Turkey Standard Time|Arab Standard Time|Belarus Standard Time|
Russian Standard Time|E. Africa Standard Time|Volgograd Standard Time|Iran Standard Time|Arabian Standard Time|
Astrakhan Standard Time|Azerbaijan Standard Time|Russia Time Zone 3|Mauritius Standard Time|Saratov Standard Time|
Georgian Standard Time|Caucasus Standard Time|Afghanistan Standard Time|West Asia Standard Time|
Ekaterinburg Standard Time|Pakistan Standard Time|Qyzylorda Standard Time|India Standard Time|
Sri Lanka Standard Time|Nepal Standard Time|Central Asia Standard Time|Bangladesh Standard Time|Omsk Standard Time|
Myanmar Standard Time|SE Asia Standard Time|Altai Standard Time|W. Mongolia Standard Time|North Asia Standard Time|
N. Central Asia Standard Time|Tomsk Standard Time|China Standard Time|North Asia East Standard Time|
Singapore Standard Time|W. Australia Standard Time|Taipei Standard Time|Ulaanbaatar Standard Time|
Aus Central W. Standard Time|Transbaikal Standard Time|Tokyo Standard Time|North Korea Standard Time|
Korea Standard Time|Yakutsk Standard Time|Cen. Australia Standard Time|AUS Central Standard Time|
E. Australia Standard Time|AUS Eastern Standard Time|West Pacific Standard Time|Tasmania Standard Time|
Vladivostok Standard Time|Lord Howe Standard Time|Bougainville Standard Time|Russia Time Zone 10|
Magadan Standard Time|Norfolk Standard Time|Sakhalin Standard Time|Central Pacific Standard Time|
Russia Time Zone 11|New Zealand Standard Time|UTC+12|Fiji Standard Time|Kamchatka Standard Time|
Chatham Islands Standard Time|UTC+13|Tonga Standard Time|Samoa Standard Time|Line Islands Standard Time"""
TIMEZONES = [t.strip() for t in TIMEZONES.replace("\n", "").split("|") if t.strip()]


def locale_label(tag):
    return "%s · %s" % (LOCALES[tag], tag) if tag in LOCALES else tag


def default_timezone():
    """The host's Windows time zone when running on Windows, else UTC."""
    if sys.platform.startswith("win"):
        try:
            tz = subprocess.run(["tzutil", "/g"], capture_output=True, text=True).stdout.strip()
            if tz in TIMEZONES:
                return tz
        except OSError:
            pass
    return "UTC"
