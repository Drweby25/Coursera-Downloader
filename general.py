import re
from urllib.parse import unquote, urlparse

# this dictionary holds language name as key and corresponding ISO language code as value
LANG_NAME_TO_CODE_MAPPING = {'Arabic': 'ar', 'Afrikaans': 'af',
                             'Bangla': 'bn', 'Burmese': 'my',
                             'Chinese (Simplified)': 'zh-Hans', 'Chinese (Traditional)': 'zh-Hant', 'Chinese': 'zh-CN',
                             'Dutch': 'nl',
                             'English': 'en',
                             'French': 'fr', 'Finnish': 'fi',
                             'Greek': 'el',
                             'Hindi': 'hi',
                             'Italian': 'it',
                             'Japanese': 'ja',
                             'Korean': 'ko',
                             'Malay': 'ml', 'Malayalam': 'ml',
                             'Portugese': 'pt',
                             'Russian': 'ru',
                             'Spanish': 'es',
                             'Tamil': 'ta', 'Telegu': 'te', 'Thai': 'th', 'Turkish': 'tr',
                             'Urdu': 'ur',
                             'Vietnamese': 'vi',
                             '-ALL AVAILABLE': 'all', '-NONE': ''}

ALLOWED_BROWSERS = ["chrome", "edge", "firefox", "brave"]

def parse_course_input(course_input):
    """Return a Coursera slug and whether it points to a multi-course program."""
    course_input = course_input.strip()
    slug_pattern = r'^[a-zA-Z0-9-]+$'
    if re.fullmatch(slug_pattern, course_input):
        return course_input, False

    parsed = urlparse(course_input)
    if not parsed.netloc.endswith('coursera.org'):
        return "", False

    parts = [unquote(part).lower() for part in parsed.path.split('/') if part]
    if len(parts) < 2:
        return "", False

    route, slug = parts[0], parts[1]
    if not re.fullmatch(slug_pattern, slug):
        return "", False

    if route == 'learn':
        return slug, False

    if route in {'specializations', 'professional-certificates'}:
        return slug, True

    return "", False


# extract class name from course home page url
def urltoclassname(homepageurl):
    return parse_course_input(homepageurl)[0]


def loadcauth(domain:str, browser:str):
    """this function returns the cauth code of browser for the specified domain.
    
    args:
        browser - must be in the ALLOWED_BROWSERS list
    
    example use: loadcauth('coursera.org'). 
    
    """
    cauth = ""

    if browser not in ALLOWED_BROWSERS:
        print(f"Browser not supported. Please login on one of these browsers: {', '.join(ALLOWED_BROWSERS)}")
        return cauth
    try:
        import rookiepy

        cookie_loader = getattr(rookiepy, browser, None)
        if cookie_loader is None:
            print(f"Browser not supported. Please login on one of these browsers: {', '.join(ALLOWED_BROWSERS)}")
            return cauth
        cookies = cookie_loader([domain])
    except Exception as e:
        print(f"Error fetching cookies: {e}")
        print(f"Could not fetch authentication. Maybe run the app as administrator.")
        return cauth

    for cookie in cookies:
            if cookie['name'] == "CAUTH":
                cauth = cookie['value']
    
    return cauth


def move_to_first(dictionary, key):
    if key not in dictionary:
        return dictionary  # Key not found, no changes needed

    value = dictionary[key]
    # Create a new dictionary with the desired key-value pair as the first item
    new_dict = {key: value}

    for k, v in dictionary.items():
        if k != key:
            # Insert the remaining key-value pairs into the new dictionary
            new_dict[k] = v

    return new_dict

# testing urltoclassname function
# url = "https://www.coursera.org/learn/model-thinking"
# url = "https://www.coursera.org/learn/model-thinking/home/week/1"
# url = "https://www.coursera.org/learn/neural-networks-deep-learning?specialization=deep-learning"
# url = "https://www.coursera.org/learn/java-programming-recommender/home/week/1https://www.coursera.org/learn/java-programming-recommender/home/week/1"
# url = "model-thinking-hell"
# url = "model-thinking?"
# cn = urltoclassname(url)
# print(cn)
if __name__ == "__main__":
    ca = loadcauth('coursera.org', browser='opera_gx')
    print(ca)
