# Tailscale Updator for Home Assistant

Tailscale tailnet 정책(JSONC/HuJSON)의 app connector 도메인을 Home Assistant에서 관리하는 custom integration입니다. OAuth Client ID/Secret으로 인증을 유지하며, 도메인별 switch와 ACL 편집 액션을 제공합니다.

## 설치 및 인증

Home Assistant **2025.4.4 이상**이 필요합니다.

1. `custom_components/tailscale_updator` 폴더 전체를 HA의 `/config/custom_components/tailscale_updator`에 복사하고 HA를 재시작합니다. 릴리스 ZIP은 이 폴더 안에 압축 해제합니다.
2. 또는 HACS → 사용자 지정 저장소에 이 저장소를 **Integration**으로 추가하고 설치합니다. GitHub에 저장소와 릴리스가 게시되어 있어야 합니다.
3. Tailscale 관리 콘솔의 **Trust credentials / OAuth clients**에서 OAuth 클라이언트를 만들고 **policy_file 쓰기** 권한을 부여합니다. 예전 UI에서는 `acl`로 표시될 수 있습니다. 장치 인증 키(`tskey-auth`)나 일반 API 키가 아닙니다.
4. HA → 설정 → 기기 및 서비스 → 통합 구성요소 추가 → **Tailscale Updator**에서 tailnet 이름, Client ID, Client Secret을 입력합니다. `-` 대신 실제 tailnet 이름을 사용합니다.
5. ACL에 이미 있는 app connector 도메인은 첫 연결 때 자동으로 switch로 생성됩니다. 통합의 **구성**에서 앱 이름을 선택해 도메인 쌍을 직접 추가·제거·이름 변경할 수 있습니다.

이 인증은 OAuth 2.0 **client credentials** 방식입니다. 브라우저 리디렉션 로그인이나 refresh token을 사용하지 않습니다. 요청 시 토큰 만료 60초 전부터 새 토큰을 발급하고, API가 401을 반환하면 한 번 재발급하여 재시도합니다. HA 재시작 후 저장된 자격 증명으로 다시 토큰을 발급합니다. 폐기된 자격 증명은 HA 재인증 흐름으로 갱신할 수 있습니다.

Client Secret은 HA config entry 저장소에 보관됩니다. 토큰은 메모리에만 보관하며, 진단 데이터·엔티티 속성에 정책 본문이나 자격 증명을 노출하지 않습니다.

## App connector 정책 예시

기존 Tailscale 정책의 `nodeAttrs`에 다음과 같은 **명시적인 domains 배열**이 있어야 합니다.

```jsonc
{
  "nodeAttrs": [
    {
      "target": ["*"],
      "app": {
        "tailscale.com/app-connectors": [
          {
            "name": "Streaming",
            "connectors": ["tag:streaming-connector"],
            "domains": ["example.com", "*.example.com"],
          },
        ],
      },
    },
  ],
}
```

이는 관련 부분만 보여주는 예시입니다. 실제 앱 커넥터 노드, 태그 소유권, 경로 승인 및 접근 권한은 Tailscale에서 별도로 구성해야 합니다. 앱 이름 `Streaming`을 선택하며 노드 호스트명이나 `tag:streaming-connector`로 선택하지 않습니다. 중복 앱 이름은 모호한 수정을 막기 위해 거부합니다. `presetAppID` 앱은 도메인을 자동 관리하므로 지원하지 않습니다.

## Switch 동작

Tailscale은 `*.example.com`에 기본 도메인 `example.com`을 포함하지 않습니다. 따라서 **한 개의 switch가 `example.com`과 `*.example.com` 쌍을 제어**합니다. 입력에 `.example.com`이나 `*.example.com`을 넣어도 `example.com`으로 정규화합니다. ACL에는 Tailscale이 사용하는 `example.com`과 `*.example.com`을 기록하며, 앞에 점만 붙인 `.example.com`은 기록하지 않습니다. 자세한 근거는 [Tailscale wildcard 설명](https://tailscale.com/docs/reference/targets-and-selectors)을 참고하세요.

- 설치 직후 각 명시적 도메인 앱의 **현재 ACL 도메인 전체**를 읽어 앱·기본 도메인별 switch를 자동 생성합니다. 외부에서 새 도메인을 추가하면 다음 폴링 때 새 switch도 나타납니다.
- **켜기**: 그 앱의 기본 도메인과 wildcard 두 항목을 모두 추가합니다. 이미 한 항목만 있으면 빠진 항목만 채웁니다.
- **끄기**: 두 항목을 함께 제거합니다. 스위치는 Off 상태로 남아 다시 켤 수 있습니다. HA 재시작 후에도 엔티티 레지스트리에서 복원됩니다.
- 두 항목이 모두 있어야 On입니다. 한 항목만 있으면 Off로 표시하고 `parent_present`·`wildcard_present` 속성으로 어느 쪽이 있는지 보여줍니다.
- 설정 저장이나 HA 시작은 ACL을 변경하지 않습니다. 실제 ACL을 읽어 상태를 정하고, 활성 엔티티가 있으면 60초마다 외부 변경을 반영합니다.
- 앱이 삭제되거나 읽기에 실패하면 스위치는 `unavailable`이 됩니다. 실패한 쓰기는 켜짐/꺼짐 성공으로 표시하지 않습니다.

HA 통합의 **구성**에서 앱과 작업을 선택합니다. **추가**는 도메인 쌍을 ACL에 저장하고 switch를 생성합니다. **제거**는 해당 쌍을 ACL에서 지우며 switch를 Off로 남깁니다. **수정**은 기존 도메인 쌍을 제거하고 새 도메인 쌍을 한 번의 ACL 쓰기로 추가합니다. 기존 앱은 Tailscale 정책에 있어야 하며, 새 앱 커넥터 자체를 이 화면에서 만들지는 않습니다.

**Off는 인터넷 접근 차단이 아닙니다.** 해당 앱의 도메인 선언을 제거하는 기능이며 다른 wildcard/앱/경로의 영향이나 이미 학습한 라우트의 즉시 철회를 보장하지 않습니다.

도메인 수정은 해당 `domains` 배열만 직렬화합니다. 배열 **안의 주석과 배치**는 변경되지만, 그 밖의 주석·들여쓰기·grants·태그·다른 앱 등은 원문 그대로 보존합니다. 전체 정책은 Tailscale API에서 검증한 뒤 저장됩니다.

모든 쓰기는 ETag/If-Match를 사용합니다. 도메인 수정 중 다른 편집이 발생하면 최신 정책을 읽고 최대 세 번 시도합니다. ETag가 없는 응답은 수정하지 않습니다. 타임아웃·429·서버 오류는 무조건 반복하지 않으며 다음 폴링이나 사용자 재시도로 상태를 확인합니다. 서버에 쓰기가 반영된 후 응답이 유실된 경우에도 오류가 표시될 수 있습니다.

## 자동화 액션

정책 액션은 관리자 사용자 또는 HA 내부 자동화에서 사용할 수 있습니다. `entry_id`는 개발자 도구 → 액션에서 통합 항목 선택기로 지정할 수 있습니다.

### 여러 도메인 추가/제거

```yaml
action: tailscale_updator.add_domains
data:
  entry_id: YOUR_CONFIG_ENTRY_ID
  connector: Streaming
  domains:
    - example.com
```

제거는 `tailscale_updator.remove_domains`를 같은 인자로 호출합니다. 기본 도메인과 wildcard가 항상 한 쌍으로 처리됩니다. 액션으로 추가한 도메인도 다음 정책 동기화 때 자동으로 switch에 등록됩니다. 빈 배열이 되어도 앱 자체는 삭제하지 않습니다.

### 전체 ACL JSONC 읽기/수정

```yaml
action: tailscale_updator.get_acl
data:
  entry_id: YOUR_CONFIG_ENTRY_ID
response_variable: current_acl
```

응답은 `policy`(JSONC 문자열)와 `etag`입니다. 원문을 별도로 보관하고 필요한 부분을 편집한 후 다음 액션으로 저장합니다. 응답을 받는 스크립트/자동화의 trace에는 정책이 포함될 수 있습니다.

```yaml
action: tailscale_updator.set_acl
data:
  entry_id: YOUR_CONFIG_ENTRY_ID
  expected_etag: '{{ current_acl.etag }}'
  policy: '{{ edited_policy }}'
```

`edited_policy`는 호출자가 준비한 **전체** 정책 문자열입니다. 이 예시는 두 변수를 준비한 스크립트 문맥에서 사용하세요. 전체 정책 교체가 충돌하면 자동 덮어쓰기 없이 실패합니다. 최신 정책을 다시 읽고 변경을 합쳐서 재시도해야 합니다. `expected_etag: '*'`는 허용하지 않습니다.

## 개발 및 검증

```sh
python3.13 -m venv .venv
. .venv/bin/activate
pip install -r requirements_test.txt
ruff check .
ruff format --check .
pytest -q
tests/docker/run_compatibility_smoke.sh
```

로컬 테스트는 HTTP 응답을 모킹해 OAuth 갱신/재인증, 충돌 재시도, JSONC 보존, 도메인 쌍, HA 설정 흐름, ACL 도메인 자동 발견, 추가·수정·제거, switch 상태, 관리자 서비스 및 설치/언로드를 검증합니다. Docker 테스트에는 Docker 데몬이 필요합니다. 현재 Home Assistant `stable` 컨테이너 안에서 로컬 가짜 Tailscale HTTP API를 띄우고, 정책 테스트와 실제 HA 설정·switch·도메인 추가·수정·외부 변경·토큰 재발급·ETag 충돌 흐름을 검사합니다. 테스트는 실제 tailnet 정책을 변경하지 않습니다. 실제 Tailscale 자격 증명을 사용하는 운영 환경 검증은 별도로 필요합니다.

`.github/workflows/tests.yml`은 로컬 기준 pytest·lint, Docker의 현재 Home Assistant 안정 버전 호환성 테스트, hassfest를 실행합니다. `release.yml`은 참고 저장소의 구조를 유지하며 master에 새 manifest 버전이 올라오면 태그/릴리스와 `tailscale_updator.zip`을 생성합니다. `stale.yaml`은 비활성 이슈를 관리합니다.

## 참고 및 라이선스

- [Virtual Layer](https://github.com/hwajin-me/virtual-layer): custom component / manifest / HACS / GitHub Actions 구조 참고. 릴리스·stale workflow를 수정하여 사용했습니다.
- [Home Assistant Tailscale](https://www.home-assistant.io/integrations/tailscale/): 기본 Tailscale 연동 참고. 이 컴포넌트는 독립된 `tailscale_updator` 도메인을 사용합니다.
- [Tailscale OAuth clients](https://tailscale.com/docs/features/oauth-clients)
- [Tailscale API](https://tailscale.com/api)
- [Tailscale app connector 설정](https://tailscale.com/docs/features/app-connectors/how-to/setup)

GPL-3.0. 참고 저장소에서 가져온 workflow와 라이선스의 원저작권을 유지합니다.
