// lokilogs — тонкий клиент к Loki по mTLS: логи подов по стенду (namespace), сервису, времени
// и фильтру — для агента и человека во время выкатки, тестов и разбора инцидентов.
//
// Почему свой бинарь, а не curl: системный macOS curl (LibreSSL) не умеет
// Ed25519-клиентсертификаты. Go crypto/tls умеет нативно. Если серверный TLS-серт
// Loki самоподписанный, верификация сервера по умолчанию выключена
// (InsecureSkipVerify) — клиентская аутентификация по mTLS при этом полноценная.
// Положите CA сервера в LOKI_SERVER_CA, и верификация включится.
//
// Конфиг берётся из окружения (обычно `. ~/.config/loki-client/env`):
//
//	LOKI_URL        https://loki.example.com
//	LOKI_CERT       путь к клиентскому серту (PEM)
//	LOKI_KEY        путь к клиентскому ключу (PEM, например Ed25519)
//	LOKI_ORG        X-Scope-OrgID (опционально, для мультитенантного Loki)
//	LOKI_SERVER_CA  путь к CA сервера (опционально; включает верификацию сервера)
//	LOKI_PRESETS    пресеты стендов и групп сервисов (JSON; по умолчанию presets.local.json
//	                рядом с бинарём; пример — presets.example.json)
package main

import (
	"crypto/tls"
	"crypto/x509"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
)

type config struct {
	baseURL  string
	cert     string
	key      string
	org      string
	serverCA string
}

func loadConfig() (config, error) {
	c := config{
		baseURL:  strings.TrimRight(os.Getenv("LOKI_URL"), "/"),
		cert:     os.Getenv("LOKI_CERT"),
		key:      os.Getenv("LOKI_KEY"),
		org:      os.Getenv("LOKI_ORG"),
		serverCA: os.Getenv("LOKI_SERVER_CA"),
	}
	if c.baseURL == "" || c.cert == "" || c.key == "" {
		return c, errors.New("нужны env LOKI_URL, LOKI_CERT, LOKI_KEY (обычно: . ~/.config/loki-client/env)")
	}
	// REPLACE-tenant — плейсхолдер из шаблона env: трактуем как «тенант не задан».
	if strings.HasPrefix(c.org, "REPLACE") {
		c.org = ""
	}
	return c, nil
}

func newClient(c config) (*http.Client, error) {
	pair, err := tls.LoadX509KeyPair(c.cert, c.key)
	if err != nil {
		return nil, fmt.Errorf("загрузка клиентского cert/key: %w", err)
	}
	tlsCfg := &tls.Config{
		Certificates:       []tls.Certificate{pair},
		InsecureSkipVerify: true, // серверный серт Loki самоподписанный; см. шапку файла
	}
	if c.serverCA != "" {
		pem, err := os.ReadFile(c.serverCA)
		if err != nil {
			return nil, fmt.Errorf("чтение LOKI_SERVER_CA: %w", err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("LOKI_SERVER_CA не содержит валидных сертификатов")
		}
		tlsCfg.RootCAs = pool
		tlsCfg.InsecureSkipVerify = false
	}
	return &http.Client{
		Timeout:   60 * time.Second,
		Transport: &http.Transport{TLSClientConfig: tlsCfg},
	}, nil
}

func (c config) get(client *http.Client, path string, q url.Values) ([]byte, error) {
	u := c.baseURL + path
	if len(q) > 0 {
		u += "?" + q.Encode()
	}
	req, err := http.NewRequest(http.MethodGet, u, nil)
	if err != nil {
		return nil, err
	}
	if c.org != "" {
		req.Header.Set("X-Scope-OrgID", c.org)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		snippet := string(body)
		if len(snippet) > 300 {
			snippet = snippet[:300]
		}
		return nil, fmt.Errorf("loki ответил %s: %s", resp.Status, strings.TrimSpace(snippet))
	}
	return body, nil
}

// ---- duration parsing (поддержка d/h/m/s) ----

func parseSince(s string) (time.Duration, error) {
	s = strings.TrimSpace(s)
	if strings.HasSuffix(s, "d") {
		n, err := strconv.Atoi(strings.TrimSuffix(s, "d"))
		if err != nil {
			return 0, err
		}
		return time.Duration(n) * 24 * time.Hour, nil
	}
	return time.ParseDuration(s)
}

// ---- пресеты: алиасы стендов, группы сервисов, авто-подбор по namespace ----
//
// Пресеты — данные вашей инфраструктуры, поэтому живут в JSON вне кода (LOKI_PRESETS или
// presets.local.json рядом с бинарём). Без файла: --stand — это просто namespace,
// --app auto — весь namespace.

type nsGroupRule struct {
	Names []string `json:"names"`
	Match string   `json:"match"`
	Group string   `json:"group"`
	re    *regexp.Regexp
}

type presetFile struct {
	StandAliases    map[string][]string `json:"stand_aliases"`
	AppGroups       map[string][]string `json:"app_groups"`
	NamespaceGroups []nsGroupRule       `json:"namespace_groups"`
	SyslogScaleApps []string            `json:"syslog_scale_apps"`
}

var presets presetFile
var presetsPath string

// loadPresets читает пресеты. Нет файла по умолчанию — не ошибка (работаем без пресетов);
// файл указан явно, но не читается, или он битый — ошибка: молча потерять алиас «prod»
// опаснее, чем упасть.
func loadPresets() error {
	path := os.Getenv("LOKI_PRESETS")
	explicit := path != ""
	if !explicit {
		exe, err := os.Executable()
		if err != nil {
			return nil
		}
		path = filepath.Join(filepath.Dir(exe), "presets.local.json")
	}
	data, err := os.ReadFile(path)
	if err != nil {
		if !explicit && errors.Is(err, os.ErrNotExist) {
			return nil
		}
		return fmt.Errorf("пресеты %s: %w", path, err)
	}
	if err := json.Unmarshal(data, &presets); err != nil {
		return fmt.Errorf("пресеты %s: %w", path, err)
	}
	for i := range presets.NamespaceGroups {
		r := &presets.NamespaceGroups[i]
		if r.Match != "" {
			re, err := regexp.Compile(r.Match)
			if err != nil {
				return fmt.Errorf("пресеты %s: namespace_groups[%d].match: %w", path, i, err)
			}
			r.re = re
		}
	}
	presetsPath = path
	return nil
}

// resolveStands разворачивает алиасы и список через запятую в namespace'ы.
func resolveStands(spec string) []string {
	var out []string
	for _, s := range strings.Split(spec, ",") {
		s = strings.TrimSpace(s)
		if s == "" {
			continue
		}
		if ns, ok := presets.StandAliases[s]; ok {
			out = append(out, ns...)
			continue
		}
		out = append(out, s)
	}
	return out
}

// classifyNamespace: к какой группе сервисов относится namespace (правила namespace_groups
// по порядку: сначала точные имена правила, потом его регулярка).
func classifyNamespace(ns string) string {
	for _, r := range presets.NamespaceGroups {
		for _, n := range r.Names {
			if n == ns {
				return r.Group
			}
		}
		if r.re != nil && r.re.MatchString(ns) {
			return r.Group
		}
	}
	return ""
}

// resolveApps: "auto" — подобрать сервисы по namespace; "all"/пусто — весь
// namespace; имя группы — развернуть; иначе список сервисов через запятую
// (группы и имена можно смешивать). Второе значение — заметка для шапки.
func resolveApps(spec string, namespaces []string) ([]string, string) {
	spec = strings.TrimSpace(spec)
	if spec == "all" || spec == "" {
		return nil, "фильтра по сервису нет: весь namespace"
	}
	if spec != "auto" {
		var out []string
		seen := map[string]bool{}
		for _, a := range strings.Split(spec, ",") {
			a = strings.TrimSpace(a)
			if a == "" {
				continue
			}
			list, isGroup := presets.AppGroups[a]
			if !isGroup {
				list = []string{a}
			}
			if isGroup && list == nil {
				return nil, "фильтра по сервису нет: весь namespace"
			}
			for _, x := range list {
				if !seen[x] {
					seen[x] = true
					out = append(out, x)
				}
			}
		}
		return out, ""
	}
	// auto
	var out []string
	seen := map[string]bool{}
	var unknown []string
	for _, ns := range namespaces {
		g := classifyNamespace(ns)
		if g == "" {
			unknown = append(unknown, ns)
			continue
		}
		for _, x := range presets.AppGroups[g] {
			if !seen[x] {
				seen[x] = true
				out = append(out, x)
			}
		}
	}
	if len(unknown) > 0 {
		return nil, "фильтра по сервису нет (namespace " + strings.Join(unknown, ",") +
			" не в пресетах): смотрю весь namespace — сузь --app, если строк много"
	}
	return out, ""
}

// ---- уровни логов: шкалы у сервисов разные ----
//
// Обычная шкала — pino: 50=error, 40=warn, 30=info (больше = серьёзнее), иногда ×1000.
// JSON-логи по RFC 5424 — 3=error, 4=warning, 6=info, 7=debug (0..7 с pino не пересекаются,
// поэтому разбираются в той же ветке).
// Часть сервисов пишет syslog-подобную шкалу ×10000: err=30000, warning=40000, info=60000,
// debug=70000 — МЕНЬШЕ = СЕРЬЁЗНЕЕ. Числа пересекаются с pino×1000 (30000 — «ошибка» у одних
// и «info» у других), поэтому и метка в выводе, и фильтр --level выбираются ПО ИМЕНИ СЕРВИСА:
// такие сервисы перечислены в syslog_scale_apps пресетов.

var frontLevelNames = map[int]string{3: "error", 4: "warn", 6: "info", 7: "debug"}
var pinoLevelNames = map[int]string{60: "fatal", 50: "error", 40: "warn", 30: "info", 20: "debug", 10: "trace"}
var rfcLevelNames = map[int]string{0: "fatal", 1: "fatal", 2: "fatal", 3: "error", 4: "warn", 5: "info", 6: "info", 7: "debug"}

// isSyslogScale: пишет ли сервис syslog-подобную шкалу ×10000 (по префиксу имени).
func isSyslogScale(app string) bool {
	for _, a := range presets.SyslogScaleApps {
		if a != "" && strings.HasPrefix(app, a) {
			return true
		}
	}
	return false
}

// levelAlternatives — во что разворачивать имя уровня для конкретной шкалы.
func levelAlternatives(level string, front bool) string {
	l := strings.ToLower(strings.TrimSpace(level))
	var alts []string
	if front {
		switch l {
		case "fatal", "error", "err", "critical":
			alts = []string{"err", "error", "3", "30000"}
		case "warn", "warning":
			alts = []string{"warn", "warning", "4", "40000"}
		case "info":
			alts = []string{"info", "6", "60000"}
		case "debug", "trace":
			alts = []string{"debug", "7", "70000"}
		}
	} else {
		switch l {
		case "fatal":
			alts = []string{"fatal", "60", "60000", "0", "1", "2"}
		case "error", "err":
			alts = []string{"error", "err", "50", "50000", "3"}
		case "warn", "warning":
			alts = []string{"warn", "warning", "40", "40000", "4"}
		case "info":
			alts = []string{"info", "30", "30000", "5", "6"}
		case "debug":
			alts = []string{"debug", "20", "20000", "7"}
		case "trace":
			alts = []string{"trace", "10", "10000"}
		}
	}
	if len(alts) == 0 {
		alts = []string{l}
	}
	return "(?i)^(" + strings.Join(alts, "|") + ")$"
}

// levelFilter собирает LogQL-фильтр, отдельный для сервисов со шкалой ×10000 и для всех прочих.
// Уровень лежит либо в корне конверта (level), либо во вложенном message-объекте
// (message_level после | json).
func levelFilter(level string) string {
	f := levelAlternatives(level, true)
	o := levelAlternatives(level, false)
	apps := presets.SyslogScaleApps
	switch len(apps) {
	case 0:
		return fmt.Sprintf(` | json | level=~%q or message_level=~%q`, o, o)
	case 1:
		return fmt.Sprintf(
			` | json | (app=%q and (level=~%q or message_level=~%q)) or (app!=%q and (level=~%q or message_level=~%q))`,
			apps[0], f, f, apps[0], o, o)
	}
	quoted := make([]string, len(apps))
	for i, a := range apps {
		quoted[i] = regexp.QuoteMeta(a)
	}
	re := strings.Join(quoted, "|")
	return fmt.Sprintf(
		` | json | (app=~%q and (level=~%q or message_level=~%q)) or (app!~%q and (level=~%q or message_level=~%q))`,
		re, f, f, re, o, o)
}

// ---- LogQL selector ----

func buildQuery(namespaces []string, apps []string, grep string, level string, raw string) (string, error) {
	if raw != "" {
		return raw, nil
	}
	if len(namespaces) == 0 {
		return "", errors.New("нужен --stand (или сырой --query)")
	}
	var sel []string
	if len(namespaces) == 1 {
		sel = append(sel, fmt.Sprintf(`namespace=%q`, namespaces[0]))
	} else {
		sel = append(sel, fmt.Sprintf(`namespace=~%q`, strings.Join(namespaces, "|")))
	}
	switch len(apps) {
	case 0:
		// без фильтра по app — весь namespace
	case 1:
		sel = append(sel, fmt.Sprintf(`app=%q`, apps[0]))
	default:
		sel = append(sel, fmt.Sprintf(`app=~%q`, strings.Join(apps, "|")))
	}
	q := "{" + strings.Join(sel, ", ") + "}"
	if level != "" {
		q += levelFilter(level)
	}
	if grep != "" {
		q += fmt.Sprintf(` |~ %q`, "(?i)"+grep)
	}
	return q, nil
}

// ---- query_range ----

type lokiResp struct {
	Status string `json:"status"`
	Data   struct {
		ResultType string `json:"resultType"`
		Result     []struct {
			Stream map[string]string `json:"stream"`
			Values [][2]string       `json:"values"`
		} `json:"result"`
		Stats struct {
			Summary struct {
				TotalLinesProcessed int     `json:"totalLinesProcessed"`
				ExecTime            float64 `json:"execTime"`
			} `json:"summary"`
		} `json:"stats"`
	} `json:"data"`
}

type entry struct {
	ts   time.Time
	app  string
	pod  string
	line string
}

// unwrap распаковывает JSON-конверт лога (vector/promtail на стендах):
// реальный текст лежит в .message, имя пода — в .kubernetes.pod_name,
// сервис — в .app_name. Возвращает (message, app, pod, true) если это конверт.
func unwrap(raw, streamApp string) (msg, app, pod string, ok bool) {
	s := strings.TrimSpace(raw)
	if !strings.HasPrefix(s, "{") {
		return "", "", "", false
	}
	var env struct {
		Message    json.RawMessage `json:"message"`
		AppName    string          `json:"app_name"`
		Kubernetes struct {
			PodName string `json:"pod_name"`
		} `json:"kubernetes"`
	}
	if err := json.Unmarshal([]byte(s), &env); err != nil {
		return "", "", "", false
	}
	appName := env.AppName
	if appName == "" {
		appName = streamApp
	}
	text := renderMessage(env.Message, appName)
	if text == "" {
		return "", "", "", false
	}
	pod = env.Kubernetes.PodName
	return text, env.AppName, pod, true
}

// renderMessage: на стендах .message — строка, на проде — вложенный объект
// (pino: msg/level/statusCode/path/...). Второй случай раньше печатался сырым
// JSON во всю ширину экрана; здесь он сводится к читаемой строке.
func renderMessage(rm json.RawMessage, app string) string {
	s := strings.TrimSpace(string(rm))
	if s == "" || s == "null" {
		return ""
	}
	if s[0] == '"' {
		var str string
		if err := json.Unmarshal(rm, &str); err == nil {
			return str
		}
		return s
	}
	if s[0] != '{' {
		return s
	}
	var m map[string]interface{}
	if err := json.Unmarshal(rm, &m); err != nil {
		return s
	}
	var head string
	for _, k := range []string{"msg", "message", "event"} {
		if v, ok := m[k]; ok {
			head = fmt.Sprintf("%v", v)
			break
		}
	}
	var parts []string
	if lv := levelLabel(m["level"], app); lv != "" {
		parts = append(parts, lv)
	}
	if head != "" {
		parts = append(parts, head)
	}
	for _, k := range []string{"component", "operation", "statusCode", "path", "host", "err", "error", "code", "reason"} {
		if v, ok := m[k]; ok {
			parts = append(parts, fmt.Sprintf("%s=%s", k, compactValue(v)))
		}
	}
	if len(parts) == 0 {
		return s
	}
	return strings.Join(parts, "  ")
}

// levelLabel печатает метку уровня ПО ШКАЛЕ КОНКРЕТНОГО СЕРВИСА:
// syslog_scale_apps — шкала ×10000 (30000=err ... 70000=debug, меньше = серьёзнее),
// остальные — pino (50=error ... 10=trace, больше = серьёзнее) и RFC 5424 (0..7).
// Без этого 5xx-ошибки сервиса со шкалой ×10000 печатались бы как [info].
func levelLabel(v interface{}, app string) string {
	if v == nil {
		return ""
	}
	front := isSyslogScale(app)
	switch t := v.(type) {
	case string:
		if t == "" {
			return ""
		}
		return "[" + strings.ToLower(t) + "]"
	case float64:
		n := int(t)
		if front {
			if n >= 10000 {
				n /= 10000
			}
			if name, ok := frontLevelNames[n]; ok {
				return "[" + name + "]"
			}
			return fmt.Sprintf("[level=%d]", int(t))
		}
		if n < 10 {
			if name, ok := rfcLevelNames[n]; ok {
				return "[" + name + "]"
			}
		}
		if n >= 1000 {
			n /= 1000
		}
		if name, ok := pinoLevelNames[n]; ok {
			return "[" + name + "]"
		}
		return fmt.Sprintf("[level=%d]", int(t))
	}
	return ""
}

// compactValue не даёт вложенным объектам развалить строку вывода.
func compactValue(v interface{}) string {
	switch t := v.(type) {
	case string:
		return t
	case float64:
		if t == float64(int64(t)) {
			return strconv.FormatInt(int64(t), 10)
		}
		return fmt.Sprintf("%v", t)
	default:
		b, err := json.Marshal(v)
		if err != nil {
			return fmt.Sprintf("%v", v)
		}
		s := string(b)
		if len(s) > 200 {
			s = s[:200] + "..."
		}
		return s
	}
}

func runQuery(args []string) error {
	fs := flag.NewFlagSet("query", flag.ContinueOnError)
	stand := fs.String("stand", "", "стенд/контур: namespace или алиас из пресетов (stand_aliases); можно через запятую")
	appCSV := fs.String("app", "auto", "сервис(ы)/группа: auto (по namespace из пресетов), all (весь ns), группа из app_groups или имена через запятую")
	since := fs.String("since", "1h", "период назад: 30m, 2h, 1d ...")
	grep := fs.String("grep", "", "regex-фильтр по строке (case-insensitive)")
	level := fs.String("level", "", "фильтр по level JSON-лога: error|warn|... (case-insensitive)")
	raw := fs.String("query", "", "сырой LogQL (переопределяет stand/app/grep/level)")
	limit := fs.Int("limit", 200, "макс. строк (cap 5000)")
	direction := fs.String("direction", "backward", "backward|forward")
	jsonOut := fs.Bool("json", false, "сырой JSON-ответ Loki")
	quiet := fs.Bool("quiet", false, "только строки логов, без шапки")
	utc := fs.Bool("utc", false, "таймстемпы в UTC (по умолчанию локальное время машины запуска)")
	noUnwrap := fs.Bool("raw", false, "не распаковывать JSON-конверт лога (показать строку как есть)")
	if err := fs.Parse(args); err != nil {
		return err
	}

	cfg, err := loadConfig()
	if err != nil {
		return err
	}
	client, err := newClient(cfg)
	if err != nil {
		return err
	}

	if err := loadPresets(); err != nil {
		return err
	}

	namespaces := resolveStands(*stand)
	apps, appNote := resolveApps(*appCSV, namespaces)
	q, err := buildQuery(namespaces, apps, *grep, *level, *raw)
	if err != nil {
		return err
	}

	dur, err := parseSince(*since)
	if err != nil {
		return fmt.Errorf("плохой --since %q: %w", *since, err)
	}
	if *limit > 5000 {
		*limit = 5000
	}
	end := time.Now()
	start := end.Add(-dur)

	vals := url.Values{}
	vals.Set("query", q)
	vals.Set("start", strconv.FormatInt(start.UnixNano(), 10))
	vals.Set("end", strconv.FormatInt(end.UnixNano(), 10))
	vals.Set("limit", strconv.Itoa(*limit))
	vals.Set("direction", *direction)

	body, err := cfg.get(client, "/loki/api/v1/query_range", vals)
	if err != nil {
		return err
	}
	if *jsonOut {
		os.Stdout.Write(body)
		return nil
	}

	var lr lokiResp
	if err := json.Unmarshal(body, &lr); err != nil {
		return fmt.Errorf("разбор ответа Loki: %w", err)
	}
	if lr.Status != "success" {
		return fmt.Errorf("loki status=%s", lr.Status)
	}

	var entries []entry
	for _, st := range lr.Data.Result {
		app := st.Stream["app"]
		pod := st.Stream["pod"]
		if pod == "" {
			pod = st.Stream["hostname"]
		}
		for _, v := range st.Values {
			ns, _ := strconv.ParseInt(v[0], 10, 64)
			line := strings.TrimRight(v[1], "\n")
			eApp, ePod := app, pod
			if !*noUnwrap {
				if msg, ua, up, ok := unwrap(line, app); ok {
					line = msg
					if ua != "" {
						eApp = ua
					}
					if up != "" {
						ePod = up
					}
				}
			}
			entries = append(entries, entry{
				ts:   time.Unix(0, ns),
				app:  eApp,
				pod:  ePod,
				line: line,
			})
		}
	}
	sort.Slice(entries, func(i, j int) bool { return entries[i].ts.Before(entries[j].ts) })

	if !*quiet {
		fmt.Fprintf(os.Stderr, "# query: %s\n", q)
		if appNote != "" {
			fmt.Fprintf(os.Stderr, "# %s\n", appNote)
		}
		fmt.Fprintf(os.Stderr, "# window: %s .. now | streams=%d lines=%d scanned=%d (%.3fs)\n",
			*since, len(lr.Data.Result), len(entries),
			lr.Data.Stats.Summary.TotalLinesProcessed, lr.Data.Stats.Summary.ExecTime)
		if len(entries) == 0 {
			fmt.Fprintln(os.Stderr, "# (пусто — стенд мог молчать в этом окне, расширь --since или сними --grep/--level)")
		}
	}
	loc := time.Local
	if *utc {
		loc = time.UTC
	}
	for _, e := range entries {
		tag := e.app
		if e.pod != "" {
			tag += "/" + e.pod
		}
		fmt.Printf("%s  [%s]  %s\n", e.ts.In(loc).Format("2006-01-02 15:04:05"), tag, e.line)
	}
	return nil
}

// ---- labels ----

func runLabels(args []string) error {
	fs := flag.NewFlagSet("labels", flag.ContinueOnError)
	name := fs.String("name", "", "если задано — значения этого лейбла (напр. namespace, app); иначе список лейблов")
	if err := fs.Parse(args); err != nil {
		return err
	}
	cfg, err := loadConfig()
	if err != nil {
		return err
	}
	client, err := newClient(cfg)
	if err != nil {
		return err
	}
	path := "/loki/api/v1/labels"
	if *name != "" {
		path = "/loki/api/v1/label/" + *name + "/values"
	}
	body, err := cfg.get(client, path, nil)
	if err != nil {
		return err
	}
	var r struct {
		Status string   `json:"status"`
		Data   []string `json:"data"`
	}
	if err := json.Unmarshal(body, &r); err != nil {
		return err
	}
	for _, v := range r.Data {
		fmt.Println(v)
	}
	return nil
}

func usage() {
	fmt.Fprint(os.Stderr, `lokilogs — логи подов из Loki по mTLS

Использование:
  lokilogs query   --stand qa1 [--app auto] [--since 1h] [--grep RE] [--level error] [--limit 200]
  lokilogs query   --query '{namespace="qa1"} |~ "(?i)timeout"' --since 30m
  lokilogs labels  [--name namespace|app]
  lokilogs presets                  # какие пресеты загружены и откуда

--stand  namespace в Loki или алиас из пресетов (stand_aliases); можно через запятую: qa1,qa2.
--app    auto (по умолчанию) — набор сервисов по namespace из пресетов (namespace_groups),
         незнакомый namespace — целиком; all — весь namespace; группа из app_groups или имена
         сервисов через запятую (можно вперемешку).

Конфиг — из env (обычно . ~/.config/loki-client/env): LOKI_URL, LOKI_CERT, LOKI_KEY, [LOKI_ORG],
[LOKI_SERVER_CA], [LOKI_PRESETS — по умолчанию presets.local.json рядом с бинарём].

--level учитывает разные шкалы: pino (50=error, 40=warn), RFC 5424 (3=error, 4=warning) и
syslog-подобную ×10000 (30000=err, меньше = серьёзнее) у сервисов из syslog_scale_apps.
Фильтр и метка в выводе выбираются по имени сервиса, вручную ничего указывать не нужно.

Примеры:
  lokilogs query --stand qa1 --since 30m --grep error
  lokilogs query --stand prod --level error --since 15m     # prod — алиас из пресетов
  lokilogs query --stand qa1 --app workers --since 1h       # workers — группа из пресетов
  lokilogs labels --name namespace
`)
}

// runPresets печатает загруженные пресеты — проверить, что подхватился нужный файл.
func runPresets() error {
	if err := loadPresets(); err != nil {
		return err
	}
	if presetsPath == "" {
		fmt.Println("пресеты не загружены: нет LOKI_PRESETS и presets.local.json рядом с бинарём")
		return nil
	}
	fmt.Println("файл:", presetsPath)
	out, _ := json.MarshalIndent(presets, "", "  ")
	fmt.Println(string(out))
	return nil
}

func main() {
	if len(os.Args) < 2 {
		usage()
		os.Exit(2)
	}
	var err error
	switch os.Args[1] {
	case "query":
		err = runQuery(os.Args[2:])
	case "labels":
		err = runLabels(os.Args[2:])
	case "presets":
		err = runPresets()
	case "-h", "--help", "help":
		usage()
		return
	default:
		usage()
		os.Exit(2)
	}
	if err != nil {
		fmt.Fprintln(os.Stderr, "ошибка:", err)
		os.Exit(1)
	}
}
