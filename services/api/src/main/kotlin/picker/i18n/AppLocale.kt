package picker.i18n

/** The four supported locales. BCP 47 tags at the API boundary, storage keys everywhere else. */
enum class AppLocale(val tag: String, val key: String, val icu: String) {
    EN("en", "en", "en"),
    RU("ru", "ru", "ru"),
    JA("ja", "ja", "ja"),
    JA_LATN("ja-Latn", "ja_latn", "ja-Latn"),
    ;

    /** Title fallback chain from the root README. */
    val titleChain: List<AppLocale>
        get() =
            when (this) {
                EN -> listOf(EN, JA_LATN, JA)
                RU -> listOf(RU, EN, JA_LATN, JA)
                JA -> listOf(JA, JA_LATN, EN)
                JA_LATN -> listOf(JA_LATN, EN, JA)
            }

    /** Synopsis fallback chain from the root README (no romanized synopses exist). */
    val synopsisChain: List<AppLocale>
        get() =
            when (this) {
                EN -> listOf(EN)
                RU -> listOf(RU, EN)
                JA -> listOf(JA, EN)
                JA_LATN -> listOf(EN)
            }

    companion object {
        fun fromTag(tag: String): AppLocale? = entries.firstOrNull { it.tag.equals(tag.trim(), ignoreCase = true) }

        fun fromKey(key: String?): AppLocale? = entries.firstOrNull { it.key == key }
    }
}

/** First non-empty value along [chain], and the locale it came from. */
fun <T : Any> pick(
    chain: List<AppLocale>,
    values: Map<AppLocale, T?>,
    empty: (T) -> Boolean = { false },
): Pair<T, AppLocale>? {
    for (locale in chain) {
        val value = values[locale] ?: continue
        if (!empty(value)) return value to locale
    }
    return null
}

fun pickText(chain: List<AppLocale>, values: Map<AppLocale, String?>): Pair<String, AppLocale>? =
    pick(chain, values) { it.isBlank() }
