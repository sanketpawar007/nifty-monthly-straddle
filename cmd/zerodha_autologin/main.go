package main

import (
	bytes "bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/http/cookiejar"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/pquerna/otp/totp"
)

var (
	BASE_URL_LOGIN = "https://kite.zerodha.com/api/login"
)

type LoginResponse struct {
	Status string `json:"status"`
	Data   struct {
		UserID    string `json:"user_id"`
		RequestID string `json:"request_id"`
	} `json:"data"`
}

type ZerodhaAuthVerifyResponse struct {
	Status  string `json:"status"`
	Data    *struct {
		AccessToken string `json:"access_token"`
	} `json:"data,omitempty"`
	Message string `json:"message,omitempty"`
}

func kiteLogin(client *http.Client, userID, password string) (string, error) {
	form := url.Values{}
	form.Set("user_id", userID)
	form.Set("password", password)
	form.Set("type", "user_id")
	req, err := http.NewRequest("POST", BASE_URL_LOGIN, bytes.NewBufferString(form.Encode()))
	if err != nil {
		return "", err
	}
	req.Header.Set("Accept", "application/json, text/plain, */*")
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Referer", "https://kite.zerodha.com/")
	req.Header.Set("User-Agent", "Mozilla/5.0")
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	var lr LoginResponse
	if err := json.Unmarshal(b, &lr); err != nil {
		return "", err
	}
	if lr.Status != "success" {
		return "", fmt.Errorf("login failed: %s", string(b))
	}
	return lr.Data.RequestID, nil
}

func performTwoFA(client *http.Client, userID, requestID, twofaValue string) ([]*http.Cookie, error) {
	form := url.Values{}
	form.Set("user_id", userID)
	form.Set("request_id", requestID)
	form.Set("twofa_value", twofaValue)
	form.Set("twofa_type", "totp")
	req, err := http.NewRequest("POST", "https://kite.zerodha.com/api/twofa", bytes.NewBufferString(form.Encode()))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("Origin", "https://kite.zerodha.com")
	req.Header.Set("Referer", "https://kite.zerodha.com/")
	req.Header.Set("User-Agent", "Mozilla/5.0")
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	u, _ := url.Parse("https://kite.zerodha.com")
	return client.Jar.Cookies(u), nil
}

func getRequestTokenWithSession(apiKey string, cookies []*http.Cookie) (string, error) {
	client := &http.Client{CheckRedirect: func(req *http.Request, via []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	req, _ := http.NewRequest("GET", "https://kite.zerodha.com/connect/login?v=3&api_key="+apiKey, nil)
	for _, c := range cookies {
		req.AddCookie(c)
	}
	req.Header.Set("User-Agent", "Mozilla/5.0")
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusFound || resp.StatusCode == http.StatusSeeOther {
		return resp.Header.Get("Location"), nil
	}
	return "", errors.New("redirect failed")
}

func followConnectFinishURL(finishURL string, cookies []*http.Cookie) (string, error) {
	client := &http.Client{CheckRedirect: func(req *http.Request, via []*http.Request) error {
		return http.ErrUseLastResponse
	}}
	req, _ := http.NewRequest("GET", finishURL, nil)
	for _, c := range cookies {
		req.AddCookie(c)
	}
	req.Header.Set("User-Agent", "Mozilla/5.0")
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode == http.StatusFound || resp.StatusCode == http.StatusSeeOther {
		return resp.Header.Get("Location"), nil
	}
	return "", errors.New("no final redirect")
}

func extractRequestToken(redirectURL string) (string, error) {
	u, err := url.Parse(redirectURL)
	if err != nil {
		return "", err
	}
	rt := u.Query().Get("request_token")
	if rt == "" {
		return "", errors.New("request_token missing")
	}
	return rt, nil
}

func sha256sum(s string) string {
	h := sha256.New()
	h.Write([]byte(s))
	return hex.EncodeToString(h.Sum(nil))
}

func getAccessToken(apiKey, appSecret, requestToken string) (string, error) {
	urlStr  := "https://api.kite.trade/session/token"
	chk     := sha256sum(apiKey + requestToken + appSecret)
	payload := fmt.Sprintf("api_key=%s&request_token=%s&checksum=%s", apiKey, requestToken, chk)
	req, _  := http.NewRequest("POST", urlStr, strings.NewReader(payload))
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	req.Header.Set("X-Kite-Version", "3")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("exchange failed: %s", string(b))
	}
	var vr ZerodhaAuthVerifyResponse
	if err := json.Unmarshal(b, &vr); err != nil {
		return "", err
	}
	if vr.Status != "success" {
		return "", fmt.Errorf("bad status: %s", string(b))
	}
	return vr.Data.AccessToken, nil
}

func main() {
	userID    := os.Getenv("KITE_USER_ID")
	password  := os.Getenv("KITE_PASSWORD")
	totpKey   := os.Getenv("KITE_TOTP_SECRET")
	appID     := os.Getenv("KITE_API_KEY")
	appSecret := os.Getenv("KITE_API_SECRET")
	if userID == "" || password == "" || totpKey == "" || appID == "" || appSecret == "" {
		log.Fatal("missing required env vars: KITE_USER_ID, KITE_PASSWORD, KITE_TOTP_SECRET, KITE_API_KEY, KITE_API_SECRET")
	}
	jar, _ := cookiejar.New(nil)
	client := &http.Client{Jar: jar}

	reqID, err := kiteLogin(client, userID, password)
	if err != nil {
		log.Fatal(err)
	}
	otp, err := totp.GenerateCode(totpKey, time.Now())
	if err != nil {
		log.Fatal(err)
	}
	cookies, err := performTwoFA(client, userID, reqID, otp)
	if err != nil {
		log.Fatal(err)
	}
	intermediate, err := getRequestTokenWithSession(appID, cookies)
	if err != nil {
		log.Fatal(err)
	}
	final, err := followConnectFinishURL(intermediate, cookies)
	if err != nil {
		log.Fatal(err)
	}
	rt, err := extractRequestToken(final)
	if err != nil {
		log.Fatal(err)
	}
	accessToken, err := getAccessToken(appID, appSecret, rt)
	if err != nil {
		log.Fatal(err)
	}
	outPath := os.Getenv("ACCESS_TOKEN_FILE")
	if outPath == "" {
		outPath = filepath.Join("secrets", "kite_access_token")
	}
	if err := os.MkdirAll(filepath.Dir(outPath), 0o755); err != nil {
		log.Fatal(err)
	}
	if err := os.WriteFile(outPath, []byte(accessToken), 0o600); err != nil {
		log.Fatal(err)
	}
	fmt.Println("ACCESS_TOKEN_SAVED", accessToken[:6]+"...")
}
