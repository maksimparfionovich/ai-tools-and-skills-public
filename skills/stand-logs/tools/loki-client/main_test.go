package main

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// usePresets загружает пресеты из файла для теста и восстанавливает пустые после.
func usePresets(t *testing.T, path string) {
	t.Helper()
	presets, presetsPath = presetFile{}, ""
	t.Setenv("LOKI_PRESETS", path)
	if err := loadPresets(); err != nil {
		t.Fatalf("loadPresets: %v", err)
	}
	t.Cleanup(func() { presets, presetsPath = presetFile{}, "" })
}

func TestWithoutPresets(t *testing.T) {
	presets = presetFile{}
	if got := resolveStands("qa1,prod"); !reflect.DeepEqual(got, []string{"qa1", "prod"}) {
		t.Fatalf("без пресетов --stand = namespace как есть, получили %v", got)
	}
	apps, note := resolveApps("auto", []string{"qa1"})
	if apps != nil || !strings.Contains(note, "не в пресетах") {
		t.Fatalf("auto без пресетов должен смотреть весь namespace: %v %q", apps, note)
	}
	if f := levelFilter("error"); strings.Contains(f, "app=") {
		t.Fatalf("без syslog_scale_apps фильтр не должен делить по сервису: %s", f)
	}
}

func TestExamplePresets(t *testing.T) {
	usePresets(t, "presets.example.json")
	if got := resolveStands("prod,qa1"); !reflect.DeepEqual(got, []string{"shop-prod-eu", "shop-prod-us", "qa1"}) {
		t.Fatalf("алиас prod: %v", got)
	}
	apps, _ := resolveApps("auto", []string{"qa12"})
	if !reflect.DeepEqual(apps, []string{"frontend", "backend"}) {
		t.Fatalf("auto по регулярке namespace_groups: %v", apps)
	}
	apps, _ = resolveApps("web,backend-mail-worker", nil)
	if !reflect.DeepEqual(apps, []string{"frontend", "backend", "backend-mail-worker"}) {
		t.Fatalf("группа + имя, без повторов: %v", apps)
	}
	if apps, note := resolveApps("all", nil); apps != nil || note == "" {
		t.Fatalf("all — весь namespace: %v %q", apps, note)
	}
	q, err := buildQuery([]string{"qa1"}, []string{"frontend", "backend"}, "timeout", "error", "")
	if err != nil {
		t.Fatal(err)
	}
	for _, part := range []string{`namespace="qa1"`, `app=~"frontend|backend"`, `(app="frontend" and`, `|~ "(?i)timeout"`} {
		if !strings.Contains(q, part) {
			t.Fatalf("в запросе нет %s: %s", part, q)
		}
	}
	if got := levelLabel(30000.0, "frontend"); got != "[error]" {
		t.Fatalf("шкала ×10000: 30000 у frontend = error, получили %s", got)
	}
	if got := levelLabel(30.0, "backend"); got != "[info]" {
		t.Fatalf("pino: 30 = info, получили %s", got)
	}
	if got := levelLabel(3.0, "backend"); got != "[error]" {
		t.Fatalf("RFC 5424: 3 = error, получили %s", got)
	}
}

func TestBrokenPresets(t *testing.T) {
	dir := t.TempDir()
	bad := filepath.Join(dir, "bad.json")
	if err := os.WriteFile(bad, []byte("{not json"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("LOKI_PRESETS", bad)
	if err := loadPresets(); err == nil {
		t.Fatal("битый файл пресетов должен давать ошибку")
	}
	t.Setenv("LOKI_PRESETS", filepath.Join(dir, "absent.json"))
	if err := loadPresets(); err == nil {
		t.Fatal("явно указанный, но отсутствующий файл пресетов должен давать ошибку")
	}
}

func TestParseSince(t *testing.T) {
	for in, want := range map[string]string{"30m": "30m0s", "2h": "2h0m0s", "1d": "24h0m0s"} {
		d, err := parseSince(in)
		if err != nil || d.String() != want {
			t.Fatalf("parseSince(%q) = %v, %v", in, d, err)
		}
	}
}
