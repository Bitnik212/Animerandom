package picker.i18n

import io.kotest.assertions.throwables.shouldThrow
import io.kotest.matchers.shouldBe
import org.junit.jupiter.api.Test
import org.junit.jupiter.params.ParameterizedTest
import org.junit.jupiter.params.provider.CsvSource
import picker.errors.ApiException

class LocaleTest {
    @ParameterizedTest
    @CsvSource(
        "ru, , , RU",
        "ja-Latn, en, , JA_LATN",
        "JA-latn, , , JA_LATN",
        ", ru-RU, en, RU",
        ", 'de-DE, ja;q=0.8, en;q=0.9', , EN",
        ", 'fr, de', ja_latn, JA_LATN",
        ", , ru, RU",
        ", , , EN",
        ", 'ja-Latn-JP', , JA_LATN",
        ", 'ja-JP,ja;q=0.9', , JA",
        ", 'en;q=0', ru, RU",
    )
    fun `lang param, Accept-Language, saved locale, default - in that order`(
        lang: String?,
        accept: String?,
        saved: String?,
        expected: AppLocale,
    ) {
        LocaleResolver.resolve(lang, accept, saved, AppLocale.EN) shouldBe expected
    }

    @Test
    fun `an unsupported lang parameter is an error, not a silent default`() {
        shouldThrow<ApiException> { LocaleResolver.resolve("de", null, null, AppLocale.EN) }.type shouldBe
            "unsupported-locale"
    }

    @Test
    fun `fallback chains follow the root README`() {
        val titles = mapOf(AppLocale.JA to "進撃の巨人", AppLocale.JA_LATN to "Shingeki no Kyojin", AppLocale.EN to "")
        pickText(AppLocale.RU.titleChain, titles) shouldBe ("Shingeki no Kyojin" to AppLocale.JA_LATN)
        pickText(AppLocale.JA.titleChain, titles) shouldBe ("進撃の巨人" to AppLocale.JA)
        pickText(AppLocale.JA_LATN.synopsisChain, mapOf(AppLocale.JA to "…")) shouldBe null
    }

    @Test
    fun `every bundle has every English key`() {
        val keys =
            listOf("reason.similar_to", "reason.liked_by_similar_users", "reason.popular_in_genre", "reason.popular")
        for (locale in AppLocale.entries) {
            for (key in keys) {
                (Messages.format(locale, key, mapOf("title" to "X", "genre" to "Y")).contains(key)) shouldBe false
            }
        }
    }

    @Test
    fun `plural rules come from ICU`() {
        Messages.format(AppLocale.EN, "count.anime", mapOf("count" to 1)) shouldBe "1 anime"
        Messages.format(AppLocale.JA, "count.anime", mapOf("count" to 3)) shouldBe "3作品"
        Messages.format(AppLocale.RU, "reason.similar_to", mapOf("title" to "Врата Штейна")) shouldBe
            "Потому что вам понравилось «Врата Штейна»"
    }
}
