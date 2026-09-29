package picker

import com.auth0.jwk.JwkProvider
import io.ktor.client.HttpClient
import io.ktor.client.engine.cio.CIO
import io.ktor.client.plugins.HttpTimeout
import io.ktor.http.HttpHeaders
import io.ktor.serialization.kotlinx.json.json
import io.ktor.server.application.Application
import io.ktor.server.application.ApplicationStopped
import io.ktor.server.application.install
import io.ktor.server.auth.authenticate
import io.ktor.server.plugins.callid.CallId
import io.ktor.server.plugins.callid.callIdMdc
import io.ktor.server.plugins.calllogging.CallLogging
import io.ktor.server.plugins.contentnegotiation.ContentNegotiation
import io.ktor.server.plugins.forwardedheaders.XForwardedHeaders
import io.ktor.server.request.path
import io.ktor.server.routing.Route
import io.ktor.server.routing.route
import io.ktor.server.routing.routing
import kotlinx.serialization.json.Json
import org.koin.core.module.Module
import org.koin.dsl.module
import org.koin.ktor.ext.get
import org.koin.ktor.plugin.KoinIsolated
import picker.auth.AUTH_USER
import picker.auth.AppUserProvisioner
import picker.auth.EnsureAppUser
import picker.auth.KeycloakClient
import picker.auth.RateLimiter
import picker.auth.installTokenValidation
import picker.auth.keycloakJwks
import picker.config.AppConfig
import picker.errors.installProblems
import picker.features.auth.AccountService
import picker.features.auth.AuthService
import picker.features.auth.accountRoutes
import picker.features.auth.adminRoutes
import picker.features.auth.authRoutes
import picker.features.meta.MetaRepository
import picker.features.meta.MetaService
import picker.features.meta.metaRoutes
import picker.features.ops.HealthCheck
import picker.features.ops.healthChecks
import picker.features.ops.opsRoutes
import picker.features.users.UserRepository
import picker.i18n.AppLocale
import picker.infra.db.Db
import picker.infra.redis.Redis
import java.util.UUID

/** Entry point for EngineMain (application.conf): configuration from the environment. */
fun Application.module() = configure(AppConfig.fromEnv())

val apiJson =
    Json {
        ignoreUnknownKeys = true
        encodeDefaults = true
        explicitNulls = true
    }

/** Dependencies. Tests pass [overrides] to replace any of them. */
fun appModule(config: AppConfig): Module =
    module {
        single { config }
        single(createdAtStart = true) {
            Db.connect(config.postgres, config.dbPoolSize).also { if (config.runMigrations) Db.migrate(it.dataSource) }
        }
        single { Redis(config.redisUrl) }
        single {
            HttpClient(CIO) {
                install(HttpTimeout) {
                    connectTimeoutMillis = 2_000
                    requestTimeoutMillis = 5_000
                }
                expectSuccess = false
            }
        }
        single<JwkProvider> { keycloakJwks(config.keycloak) }
        single { UserRepository(get()) }
        single { MetaRepository(get()) }
        single { MetaService(get()) }
        single { KeycloakClient(config.keycloak, get()) }
        single { RateLimiter(get()) }
        single { AuthService(config, get(), get(), get(), AppLocale.fromTag(config.defaultLocale) ?: AppLocale.EN) }
        single { AccountService(config, get(), get(), get(), get()) }
        single<List<HealthCheck>> {
            healthChecks(get(), get(), get(), config.elasticsearchUrl, config.keycloak.certsUrl, config.recEngineUrl)
        }
    }

fun Application.configure(config: AppConfig, overrides: Module? = null) {
    // Isolated: each application (and each test) gets its own container.
    install(KoinIsolated) {
        allowOverride(true)
        modules(listOfNotNull(appModule(config), overrides))
    }
    install(ContentNegotiation) { json(apiJson) }
    // Only behind a trusted proxy: otherwise any client could pick its own IP and dodge per-IP limits.
    if (config.trustForwardedHeaders) install(XForwardedHeaders) { useLastProxy() }
    install(CallId) {
        header(HttpHeaders.XRequestId)
        generate { UUID.randomUUID().toString() }
        verify { it.isNotBlank() && it.length <= 64 }
    }
    // Request bodies are never logged, so passwords on /v1/auth/* can't leak into logs.
    install(CallLogging) {
        callIdMdc("request_id")
        filter { !it.request.path().startsWith("/healthz") }
    }
    installProblems()
    installTokenValidation(config.keycloak, get(), config.jwtLeewaySeconds)

    val defaultLocale = AppLocale.fromTag(config.defaultLocale) ?: AppLocale.EN
    val users = get<UserRepository>()
    val provisioner = AppUserProvisioner { principal -> users.ensure(principal.id, defaultLocale.key) }

    // Close through direct references: the container may already be stopped by then.
    val closeables = listOf<AutoCloseable>(get<Db>(), get<Redis>(), get<HttpClient>())
    monitor.subscribe(ApplicationStopped) { closeables.forEach { runCatching { it.close() } } }

    routing {
        opsRoutes(get())
        route("/v1") {
            // Public routes: a token is optional, but an invalid one is still a 401.
            authRoutes(get())
            publicRoutes(provisioner) {
                metaRoutes(get(), users, defaultLocale)
            }
            protectedRoutes(provisioner) {
                accountRoutes(get())
                adminRoutes(get())
            }
        }
    }
}

/** Optional authentication plus app_user provisioning for the enclosed routes. */
fun Route.publicRoutes(provisioner: AppUserProvisioner, build: Route.() -> Unit) {
    authenticate(AUTH_USER, optional = true) {
        install(EnsureAppUser) { this.provisioner = provisioner }
        build()
    }
}

/** Required authentication plus app_user provisioning for the enclosed routes. */
fun Route.protectedRoutes(provisioner: AppUserProvisioner, build: Route.() -> Unit) {
    authenticate(AUTH_USER) {
        install(EnsureAppUser) { this.provisioner = provisioner }
        build()
    }
}
