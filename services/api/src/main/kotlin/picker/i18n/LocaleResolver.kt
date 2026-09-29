package picker.i18n

import picker.errors.Errors

/**
 * Per request, first match wins: `lang` parameter, `Accept-Language`, the user's saved
 * locale, the default.
 */
object LocaleResolver {
    fun resolve(langParam: String?, acceptLanguage: String?, userLocale: String?, default: AppLocale): AppLocale {
        if (!langParam.isNullOrBlank()) {
            return AppLocale.fromTag(langParam) ?: throw Errors.unsupportedLocale(langParam)
        }
        fromAcceptLanguage(acceptLanguage)?.let { return it }
        AppLocale.fromKey(userLocale)?.let { return it }
        return default
    }

    /** Best supported match for an Accept-Language header, honoring q-values. */
    fun fromAcceptLanguage(header: String?): AppLocale? {
        if (header.isNullOrBlank()) return null
        return header
            .split(",")
            .mapNotNull { part ->
                val pieces = part.trim().split(";")
                val tag = pieces[0].trim()
                val q =
                    pieces
                        .drop(1)
                        .map { it.trim() }
                        .firstOrNull { it.startsWith("q=") }
                        ?.removePrefix("q=")
                        ?.toDoubleOrNull() ?: 1.0
                matchTag(tag)?.let { it to q }
            }.filter { it.second > 0 }
            .maxByOrNull { it.second }
            ?.first
    }

    /** `ja-Latn-JP` → ja-Latn, `ja-JP` → ja, `ru-RU` → ru, `en-GB` → en. */
    internal fun matchTag(tag: String): AppLocale? {
        val parts = tag.lowercase().split("-", "_")
        return when (parts.firstOrNull()) {
            "ja" -> if ("latn" in parts.drop(1)) AppLocale.JA_LATN else AppLocale.JA
            "ru" -> AppLocale.RU
            "en" -> AppLocale.EN
            else -> null
        }
    }
}
