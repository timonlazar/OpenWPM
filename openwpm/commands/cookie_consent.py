from openwpm.commands.types import BaseCommand
from selenium.webdriver.common.by import By
from urllib.parse import urlparse
import time
import traceback

from openwpm.commands.utils.cookieButton_rules import rules
from openwpm.commands.utils.cookie_selectors import generate_xpaths


class AcceptCookieConsentCommand(BaseCommand):
    MAX_RUNTIME_SECONDS = 10

    CMP_SELECTORS = [
        "//button[@id='onetrust-accept-btn-handler']",
        "//button[@id='CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll']",
        "//button[contains(@class,'qc-cmp2-summary-buttons')]"
    ]

    def execute(self, webdriver, browser_params, manager_params, extension_socket):
        start_time = time.monotonic()

        try:
            current_url = webdriver.current_url
            domain = self.get_domain(current_url)

            print(f"[CookieConsent] START | url={current_url} | domain={domain}")

            rule = self.find_matching_rule(domain)

            for xpath in self.CMP_SELECTORS:
                runtime = time.monotonic() - start_time
                if runtime > self.MAX_RUNTIME_SECONDS:
                    print(f"[CookieConsent] TIMEOUT during CMP selectors | url={current_url} | runtime={runtime:.2f}s")
                    return
                print(f"[CookieConsent] Trying CMP selector #{self.CMP_SELECTORS.index(xpath)+1}/{len(self.CMP_SELECTORS)}")
                if self.try_click_fast(webdriver, xpath):
                    runtime = time.monotonic() - start_time
                    print(f"[CookieConsent] ✓ SUCCESS: CMP selector worked | runtime={runtime:.2f}s")
                    return
                print(f"[CookieConsent] CMP selector #{self.CMP_SELECTORS.index(xpath)+1} did not work")

            xpaths = generate_xpaths()
            print(f"[CookieConsent] Trying {len(xpaths)} keyword-based xpaths")
            for idx, xpath in enumerate(xpaths, 1):
                runtime = time.monotonic() - start_time
                if runtime > self.MAX_RUNTIME_SECONDS:
                    print(f"[CookieConsent] TIMEOUT during keyword fallback | url={current_url} | runtime={runtime:.2f}s")
                    return
                if self.try_click_fast(webdriver, xpath):
                    runtime = time.monotonic() - start_time
                    print(f"[CookieConsent] ✓ SUCCESS: Keyword xpath #{idx} worked | runtime={runtime:.2f}s")
                    return

            if rule:
                print(f"[CookieConsent] Domain rule found for {domain}: keys={list(rule.keys())}")
                result = self.apply_domain_rule(webdriver, rule)
                if result and result.get("clicked"):
                    runtime = time.monotonic() - start_time
                    print(f"[CookieConsent] ✓ SUCCESS: Domain rule worked | method={result.get('method')} | runtime={runtime:.2f}s")
                    return
                else:
                    print(f"[CookieConsent] Domain rule failed to click")
            else:
                print(f"[CookieConsent] No domain rule matched for {domain}")

            total_runtime = time.monotonic() - start_time
            print(f"[CookieConsent] No consent button found or clicked | url={current_url} | runtime={total_runtime:.2f}s")

        except Exception as e:
            print(f"[CookieConsent] ERROR | url={webdriver.current_url} | error={str(e)}")
            print(traceback.format_exc())
            return

    def get_domain(self, url):
        netloc = urlparse(url).netloc.lower()
        return netloc.replace("www.", "")

    def find_matching_rule(self, domain):
        for rule_domain in rules:
            if domain == rule_domain or domain.endswith("." + rule_domain):
                return rules[rule_domain]
        return None

    def click_and_wait(self, webdriver, element, prefer_js=False):
        """
        Attempt to click the provided element. If prefer_js is True, try JS click first.
        After a successful click, wait 5 seconds.
        Returns (clicked: bool, method: str|None)
        method is 'selenium' or 'js'.
        """
        try:
            element_text = (element.text or "")[:50]
            element_tag = element.tag_name
            
            if prefer_js:
                try:
                    print(f"[CookieConsent] Attempting JS click | tag={element_tag} | text='{element_text}'")
                    webdriver.execute_script("arguments[0].click();", element)
                    method = "js"
                    print(f"[CookieConsent] ✓ JS click succeeded | tag={element_tag}")
                except Exception as e:
                    print(f"[CookieConsent] JS click failed: {str(e)}, trying Selenium click")
                    element.click()
                    method = "selenium"
                    print(f"[CookieConsent] ✓ Selenium click succeeded | tag={element_tag}")
            else:
                try:
                    print(f"[CookieConsent] Attempting Selenium click | tag={element_tag} | text='{element_text}'")
                    element.click()
                    method = "selenium"
                    print(f"[CookieConsent] ✓ Selenium click succeeded | tag={element_tag}")
                except Exception as e:
                    print(f"[CookieConsent] Selenium click failed: {str(e)}, trying JS click")
                    webdriver.execute_script("arguments[0].click();", element)
                    method = "js"
                    print(f"[CookieConsent] ✓ JS click succeeded | tag={element_tag}")
            
            # post-click delay
            print(f"[CookieConsent] Waiting 5s after click (method={method})")
            time.sleep(5)
            return True, method
        except Exception as e:
            print(f"[CookieConsent] ✗ click_and_wait failed completely: {str(e)}")
            return False, None

    def apply_domain_rule(self, webdriver, rule):
        if isinstance(rule, dict):
            rule_xpaths = rule.get("xpaths")
            xpaths = rule_xpaths if rule_xpaths else generate_xpaths()
        else:
            # If rule is None or of an unexpected type, fall back to default xpaths
            xpaths = generate_xpaths()

        print(f"[CookieConsent] apply_domain_rule: Trying {len(xpaths)} xpaths from rule")

        # 1) Try Selenium-native clicks (with JS fallback) and wait after each click
        for xpath_idx, xp in enumerate(xpaths):
            try:
                els = webdriver.find_elements(By.XPATH, xp)
                if not els:
                    print(f"[CookieConsent]   Rule XPath #{xpath_idx+1}: NO elements found")
                    continue
                
                print(f"[CookieConsent]   Rule XPath #{xpath_idx+1}: Found {len(els)} element(s)")
                for el_idx, el in enumerate(els):
                    try:
                        if not el.is_displayed():
                            print(f"[CookieConsent]     Element #{el_idx+1} is not visible, skipping")
                            continue
                        
                        print(f"[CookieConsent]     Element #{el_idx+1}: Attempting click | tag={el.tag_name} | text='{el.text[:30] if el.text else ''}'")
                        clicked, method = self.click_and_wait(webdriver, el, prefer_js=False)
                        if clicked:
                            print(f"[CookieConsent]     ✓ Element #{el_idx+1} clicked successfully (method={method})")
                            return {"clicked": True, "xpath": xp, "method": method}
                        else:
                            print(f"[CookieConsent]     ✗ Element #{el_idx+1} click failed (Selenium), trying JS click")
                            clicked, method = self.click_and_wait(webdriver, el, prefer_js=True)
                            if clicked:
                                print(f"[CookieConsent]     ✓ Element #{el_idx+1} clicked successfully via JS (method={method})")
                                return {"clicked": True, "xpath": xp, "method": method}
                            else:
                                print(f"[CookieConsent]     ✗ Element #{el_idx+1} click failed (JS)")
                    except Exception as e:
                        print(f"[CookieConsent]     ✗ Error with element #{el_idx+1}: {str(e)}")
                        continue
            except Exception as e:
                print(f"[CookieConsent]   Rule XPath #{xpath_idx+1}: Query failed: {str(e)}")
                continue

        print(f"[CookieConsent] apply_domain_rule: No successful click via Selenium/direct clicks")
        # 2) Fallback: evaluate XPaths in-page via a safe single script with arguments
        js_code = """
        const xpaths = arguments[0];
        for (const xp of xpaths) {
          try {
            const res = document.evaluate(xp, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null);
            const node = res && res.singleNodeValue;
            if (node) {
              // Visibility / interactability checks similar to Selenium's is_displayed():
              // - element is in the DOM
              // - computed style not display:none or visibility:hidden
              // - opacity > 0
              // - has non-zero bounding rect
              // - has an offsetParent (rough proxy for being rendered)
              const inDOM = document.documentElement.contains(node);
              const style = window.getComputedStyle(node);
              const rect = node.getBoundingClientRect();
              const visible = inDOM &&
                              style &&
                              style.display !== 'none' &&
                              style.visibility !== 'hidden' &&
                              parseFloat(style.opacity || "1") > 0 &&
                              rect.width > 0 &&
                              rect.height > 0;
              const hasOffsetParent = node.offsetParent !== null || node === document.body;
              if (!visible || !hasOffsetParent) {
                continue;
              }
              if (typeof node.click === 'function') { node.click(); return {clicked: true, xpath: xp, method: 'js-eval'}; }
              const ev = new MouseEvent('click', {bubbles: true, cancelable: true, view: window});
              node.dispatchEvent(ev);
              return {clicked: true, xpath: xp, method: 'js-event'};
            }
          } catch (e) {
            // continue
          }
        }
        return {clicked: false};
        """
        try:
            print(f"[CookieConsent] apply_domain_rule: Trying JS-based click fallback")
            result = webdriver.execute_script(js_code, xpaths)
            if result and result.get("clicked"):
                # page-internal click dispatched; ensure post-click wait
                print(f"[CookieConsent] ✓ JS fallback click succeeded (method={result.get('method')})")
                time.sleep(5)
                return {
                    "clicked": True,
                    "xpath": result.get("xpath"),
                    "method": result.get("method")
                }
            # normalize failure return to include same keys as successful case
            print(f"[CookieConsent] ✗ JS fallback click failed")
            return {"clicked": False, "xpath": None, "method": None}
        except Exception as e:
            print(f"[CookieConsent] ✗ JS fallback error: {str(e)}")
            return {"clicked": False, "xpath": None, "method": None}

    def try_click_fast(self, webdriver, xpath):
        try:
            elements = webdriver.find_elements(By.XPATH, xpath)
            if not elements:
                # Don't print the full XPath, it's too long and repetitive
                return False
                
            # Only show abbreviated XPath for successful finds
            xpath_short = xpath[:80] + "..." if len(xpath) > 80 else xpath
            print(f"[CookieConsent] XPath found {len(elements)} element(s)")
            
            for idx, el in enumerate(elements):
                try:
                    is_displayed = el.is_displayed()
                    element_text = (el.text or "")[:50]
                    print(f"[CookieConsent]   Element #{idx+1}: tag={el.tag_name} | text='{element_text}' | visible={is_displayed}")
                    
                    if not is_displayed:
                        print(f"[CookieConsent]   Element #{idx+1} is NOT visible, skipping")
                        continue
                    
                    # prefer JS for fast attempts (use click_and_wait with prefer_js=True)
                    clicked, method = self.click_and_wait(webdriver, el, prefer_js=True)
                    if clicked:
                        print(f"[CookieConsent] ✓ Successfully clicked element #{idx+1} using {method}")
                        return True
                    else:
                        print(f"[CookieConsent] ✗ Click on element #{idx+1} failed")
                except Exception as e:
                    print(f"[CookieConsent] ✗ Error processing element #{idx+1}: {str(e)}")
                    continue
                    
        except Exception as e:
            print(f"[CookieConsent] ✗ XPath query failed: {str(e)}")
        

        return False
