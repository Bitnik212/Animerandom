package picker.i18n

import com.ibm.icu.text.MessageFormat
import com.ibm.icu.util.ULocale
import java.util.Properties
import java.util.concurrent.ConcurrentHashMap

/**
 * UI strings from `messages/messages_{locale}.properties` (UTF-8), formatted with ICU
 * MessageFormat so plurals follow each language's rules. A key missing in a locale falls
 * back to English; a key missing everywhere is a bug and throws.
 */
object Messages {
    private val bundles = ConcurrentHashMap<AppLocale, Properties>()

    private fun bundle(locale: AppLocale): Properties =
        bundles.getOrPut(locale) {
            val file = "messages/messages_${locale.icu.replace('-', '_')}.properties"
            Properties().apply {
                val stream =
                    Messages::class.java.classLoader.getResourceAsStream(file)
                        ?: error("Missing message bundle $file")
                stream.reader(Charsets.UTF_8).use { load(it) }
            }
        }

    fun has(key: String): Boolean = bundle(AppLocale.EN).containsKey(key)

    fun format(locale: AppLocale, key: String, args: Map<String, Any?> = emptyMap()): String {
        val pattern =
            bundle(locale).getProperty(key)
                ?: bundle(AppLocale.EN).getProperty(key)
                ?: error("Message key '$key' is missing from messages_en.properties")
        return MessageFormat(pattern, ULocale.forLanguageTag(locale.icu)).format(args)
    }
}
