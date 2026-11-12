"""Tanka Hub manages files, inventory, budgets, and tasks for a 3D printing shop."""

from dataclasses import dataclass, field as data_field
from datetime import date
from calendar import monthrange
from decimal import Decimal, InvalidOperation
import base64
import re
import sys
import csv
import io
from copy import deepcopy
from bakery import assert_equal
from drafter import (
    Button, CheckBox, Div, Download, DrafterBinaryFile, FileUpload,
    Header, Image, Label, Page, SelectBox, Text, TextArea, TextBox, DateInput,
    add_website_css, route, set_site_information, set_website_title,
    start_server,
)

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_SESSION_BYTES = 30 * 1024 * 1024
# Parentheses group these menu options into a tuple. Its order stays fixed when forms reuse it.
FREQUENCIES = ("Once", "Monthly", "Yearly","Weekly")
LIFECYCLES = ("Active", "Retired", "Discontinued", "Testing")


# The dataclass decorator builds an initializer from the typed fields below.
# Creating a ModelFile supplies those values in field order or by name.
@dataclass
class ModelFile:
    """One uploaded version, including exact bytes for downloading."""

    uid: int
    filename: str
    # The download uses these original bytes so binary model files keep their exact contents.
    # bytes holds a sequence of values from 0 to 255. Decoding a binary model as text could lose data.
    content: bytes
    category: str
    version: str
    notes: str
    active: bool
    lifecycle: str = "Active"


@dataclass
class StockItem:
    """A grouped filament type or a finished product/consumable."""

    uid: int
    name: str
    category: str
    quantity: int
    target: int
    price: int
    brand: str
    material: str
    color: str
    location: str
    notes: str
    image: str
    file_id: int
    lifecycle: str = "Active"


@dataclass
class Todo:
    """One simple task with a completion checkbox."""

    uid: int
    text: str
    done: bool = False


@dataclass
class Entry:
    """A real transaction or a planned commitment; money is integer cents."""

    uid: int
    description: str
    category: str
    amount: int
    kind: str
    status: str
    frequency: str
    due: str
    end: str
    party: str
    notes: str
    active: bool
    anchor_day: int
    anchor_month: int


@dataclass
class State:
    """All mutable application data, including four independently edited lists."""

    files: list[ModelFile]
    filaments: list[StockItem]
    products: list[StockItem]
    entries: list[Entry]
    next_id: int
    opening_balance: int
    report_date: str
    message: str
    file_query: str
    file_status: str
    filament_query: str
    product_query: str
    product_category: str
    product_price: str
    budget_query: str
    # Each new workspace gets its own task list, form draft, and pending CSV rows.
    # data_field is our local name for dataclasses.field. default_factory=list calls list()
    # for each new State, so adding a task in one session affects only that session.
    todos: list[Todo] = data_field(default_factory=list)
    draft: dict = data_field(default_factory=dict)
    pending_rows: list[dict] = data_field(default_factory=list)
    pending_kind: str = ""
    pending_mode: str = "Replace counts"


def new_state() -> State:
    """Create an empty workspace, avoiding invented business records."""
    return State([], [], [], [], 1, 0, date.today().isoformat(), "",
                 "", "All", "", "", "All", "All", "")


def allocate_id(state: State) -> int:
    """Allocate a stable identifier that is not a changing list position."""
    result = state.next_id
    state.next_id += 1
    return result


def money(cents: int) -> str:
    """Format integer cents for display."""
    return f"${cents / 100:,.2f}"


def parse_money(value: str, signed: bool = False) -> int:
    """Convert dollars to cents without floating-point rounding.

    Decimal and exception handling are advanced: Decimal reads the exact
    typed decimal, and ValueError lets routes show a useful validation message.
    """
    try:
        # Decimal reads the typed digits directly. For example, 0.29 dollars becomes exactly 29 cents.
        number = Decimal(value.strip())
        if not number.is_finite() or abs(number) > Decimal("999999999"):
            raise ValueError("Enter a finite amount below one billion dollars.")
        if number < 0 and not signed:
            raise ValueError("Amounts cannot be negative.")
        cents = number * 100
        if cents != cents.to_integral_value():
            raise ValueError("Use no more than two decimal places.")
        return int(cents)
    except InvalidOperation:
        raise ValueError("Enter a dollar amount such as 25.00.") from None


def whole_number(value: str) -> int:
    """Require a whole, nonnegative inventory count."""
    if not value.strip().isdigit():
        raise ValueError("Stock and target must be whole numbers of zero or more.")
    return int(value)


def valid_date(value: str) -> date:
    """Validate an ISO date instead of silently accepting impossible dates."""
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError()
        return result
    except ValueError:
        raise ValueError("Use a valid date in YYYY-MM-DD format.") from None


def matches(query: str, text: str) -> bool:
    """Search without case sensitivity or incidental surrounding spaces."""
    return query.strip().casefold() in text.casefold()


def version_key(item: ModelFile) -> tuple:
    """Sort filenames and versions naturally, so v2 precedes v10.

    Regular expressions split numbers from text; tagged tuples ensure Python
    never compares an integer directly to a string during sorting.
    """
    pieces = []
    # In this regular expression, \d+ matches a run of digits. Parentheses keep that run
    # in the split result so v10 can be compared using the number 10.
    for part in re.split(r"(\d+)", item.version.casefold()):
        if part.isdigit():
            pieces.append((1, int(part)))
        else:
            pieces.append((0, part))
    return (item.filename.casefold(), pieces, item.uid)


def test_helper_examples() -> None:
    """Check how a shop price displays and how a material search matches."""
    assert_equal(money(2599), "$25.99", exact_strings=True)
    assert_equal(parse_money(" 25.50 "), 2550)
    assert_equal(matches(" pla ", "Bambu PLA Black"), True)
    assert_equal(matches("PETG", "Bambu PLA Black"), False)


def field(label: str, component: object) -> Div:
    """Keep form labels visible and consistently spaced."""
    # Drafter supplies an internal field name as the default accessible name.
    # Override it with the visible wording for screen readers and UI tests.
    component.extra_settings["aria-label"] = label
    return Div([Label(label, component), component], classes="field")


def shell(state: State, title: str, content: list) -> Page:
    """Wrap each distinct page in shared navigation and a session notice."""
    navigation = Div([
        Button("Overview", index), Button("Files", files),
        Button("Filament", filament), Button("Products", products),
        Button("Budget", budget), Button("To-Do", todos),
    ], classes="nav")
    body = [Div("TANKA / BUSINESS WORKSPACE", classes="brand"),
            navigation, Header(title)]
    if state.message:
        body.append(Div(state.message, classes="message", role="status"))
    # After a validation error, put the previous answers back into the form.
    form_page = title.startswith(("Add ", "Edit ", "Upload model"))
    if form_page and state.draft:
        restore_form(content, state.draft)
    elif not form_page:
        state.draft = {}
    body.extend(content)
    return Page(state, [Div(body, classes="hub")])


def metric(label: str, value: str) -> Div:
    """Render a dashboard statistic."""
    return Div([Text(label), Div(value, classes="metric")], classes="card")


def totals(state: State) -> tuple[int, int, int]:
    """Total paid transactions through the report date, excluding plans."""
    income = 0
    expenses = 0
    for entry in state.entries:
        if entry.status == "Paid" and entry.due <= state.report_date:
            if entry.kind == "Income":
                income += entry.amount
            else:
                expenses += entry.amount
    return income, expenses, state.opening_balance + income - expenses


# Drafter sends the fields on the current form with navigation clicks. **unused accepts those extra values.
@route
def index(state: State,
                **unused: object) -> Page:
    """Show real workspace totals and stock that needs attention."""
    spool_count = 0
    product_count = 0
    low_stock = []
    for item in state.filaments:
        spool_count += item.quantity
        if item.quantity < item.target:
            low_stock.append(Text(f"Filament · {item.name}: {item.quantity} / {item.target}"))
    for item in state.products:
        product_count += item.quantity
        if item.quantity < item.target:
            low_stock.append(Text(f"Product · {item.name}: {item.quantity} / {item.target}"))
    # Unpack the three amounts returned together as a tuple for the dashboard.
    income, expenses, balance = totals(state)
    return shell(state, "Your workshop, at a glance", [
        Text("Files, materials, products and money — one place to keep track."),
        Div([metric("Stored file versions", str(len(state.files))),
             metric("Filament spools", str(spool_count)),
             metric("Product / consumable units", str(product_count)),
             metric("Recorded balance", money(balance))], classes="cards"),
        Div([Header("Stock to replenish", 2)] + (low_stock or [Text("No low-stock items.")]), classes="card"),
        Text(f"Through {state.report_date}: income {money(income)} · paid expenses {money(expenses)}"),
    ])


@route
def files(state: State,
                **unused: object) -> Page:
    """List searchable file versions with original-byte downloads."""
    cards = []
    for item in sorted(state.files, key=version_key):
        searchable = f"{item.filename} {item.category} {item.version} {item.notes}"
        status = item.lifecycle if item.active else "Retired"
        if matches(state.file_query, searchable) and state.file_status in ("All", status):
            cards.append(Div([
                Div(f"{item.category} · {status}", classes="tag"), Header(item.filename, 3),
                Text(f"Version {item.version} · {len(item.content):,} bytes"), Text(item.notes),
                (Download("Download original", item.filename, item.content,
                          content_type="application/octet-stream") if item.content
                 else Text("Catalog details only — upload the actual model to enable download.")),
                Button("Edit details", file_form, {"uid": item.uid}),
            ], classes="card " + status.lower()))
    return shell(state, "File library", [
        Button("Upload a file", file_form, {"uid": 0}),
        exchange_controls(state, "files"),
        Button("Upload multiple files", batch_file_form),
        Div([field("Search files, versions or notes", TextBox("query", state.file_query)),
             field("Status", SelectBox("status", ["All"] + list(LIFECYCLES), state.file_status)),
             Button("Search files", search_files)], classes="filters"),
        Div(cards or [Text("No files match. Upload an STL, 3MF or G-code file to begin.")], classes="cards"),
    ])


@route
def search_files(state: State, query: str, status: str,
                **unused: object) -> Page:
    """Update file filters without changing stored file contents."""
    state.file_query = query
    state.file_status = status if status in ("All",) + LIFECYCLES else "All"
    state.message = ""
    return files(state)


def test_pages_and_forms() -> None:
    """Every main page and empty entry form returns a valid Page."""
    state = new_state()
    for function in (index, files, filament, products, budget):
        assert_equal(isinstance(function(state), Page), True, exact_strings=True)
    assert_equal(isinstance(file_form(state, 0), Page), True, exact_strings=True)
    assert_equal(isinstance(stock_form(state, 'filament', 0), Page), True, exact_strings=True)
    assert_equal(isinstance(stock_form(state, 'product', 0), Page), True, exact_strings=True)
    assert_equal(isinstance(entry_form(state, 0), Page), True, exact_strings=True)


def get_file(state: State, uid: int) -> ModelFile | None:
    """Find a file by its stable identifier."""
    for item in state.files:
        if item.uid == uid:
            return item
    return None


@route
def file_form(state: State, uid: int,
                **unused: object) -> Page:
    """Display upload or metadata editing; existing bytes stay unchanged."""
    item = get_file(state, uid)
    if uid and item is None:
        state.message = "That file no longer exists."
        return files(state)
    content = [field("Category", TextBox("category", item.category if item else "Models")),
               field("Version label", TextBox("version", item.version if item else "v1")),
               field("Current use / notes", TextArea("notes", item.notes if item else "")),
               *lifecycle_fields(item.lifecycle if item else "Active")]
    if item:
        content.extend([Text(item.filename),
                        Button("Save details", save_file_details, {"uid": uid})])
    else:
        content.extend([field("Choose STL, 3MF or G-code (up to 10 MiB)",
                              FileUpload("upload", accept=".stl,.3mf,.gcode")),
                        Button("Store this version", upload_file)])
    content.append(Button("Back to files", files))
    return shell(state, "Edit file" if item else "Upload model file", [Div(content, classes="card")])


@route
def upload_file(state: State, upload: DrafterBinaryFile | None,
                category: str, version: str, notes: str, active: bool, retired: bool = False,
                discontinued: bool = False, testing: bool = False,
                **unused: object) -> Page:
    """Validate an upload, then retain its exact bytes in session state."""
    # locals() gives the form helper a dictionary of this call's names and values.
    # The helper picks out editable values so the user can correct one field and resubmit.
    remember_form(state, "file", locals())
    try:
        lifecycle = chosen_lifecycle(active, retired, discontinued, testing)
    except ValueError as error:
        state.message = str(error)
        return file_form(state, 0)
    used = sum(len(item.content) for item in state.files)
    if upload is None:
        state.message = "Choose a file first."
    elif not upload.filename.lower().endswith((".stl", ".3mf", ".gcode")):
        state.message = "Choose an STL, 3MF or G-code file."
    elif not upload.content or len(upload.content) > MAX_FILE_BYTES:
        state.message = "Choose a nonempty file no larger than 10 MiB."
    elif used + len(upload.content) > MAX_SESSION_BYTES:
        state.message = "This prototype allows 30 MiB of model uploads per session."
    elif not category.strip() or not version.strip():
        state.message = "Category and version are required."
    else:
        # Normalize path separators: retain the filename, never a local path.
        filename = upload.filename.replace("\\", "/").split("/")[-1]
        for old in state.files:
            if old.filename == filename and old.version == version.strip() and not old.content:
                old.content = upload.content
                state.message = "Model attached to its imported catalog record."
                state.draft = {}
                return files(state)
        state.files.append(ModelFile(allocate_id(state), filename, upload.content,
                                     category.strip(), version.strip(), notes.strip(),
                                     lifecycle != "Retired", lifecycle))
        state.draft = {}
        state.message = "File version stored for this session."
        return files(state)
    return file_form(state, 0)


@route
def save_file_details(state: State, uid: int, category: str,
                      version: str, notes: str, active: bool, retired: bool = False,
                discontinued: bool = False, testing: bool = False,
                **unused: object) -> Page:
    """Edit version metadata or retire a file without destroying it."""
    remember_form(state, "file", locals())
    try:
        lifecycle = chosen_lifecycle(active, retired, discontinued, testing)
    except ValueError as error:
        state.message = str(error)
        return file_form(state, uid)
    item = get_file(state, uid)
    if item is None or not category.strip() or not version.strip():
        state.message = "Select an existing file and fill in category and version."
    else:
        item.category, item.version = category.strip(), version.strip()
        item.notes, item.active = notes.strip(), lifecycle != "Retired"
        item.lifecycle = lifecycle
        state.message = "File details updated."
    return files(state)


def test_product_setup(state: State, price: str='25.00') -> None:
    """Create a known product through its real route."""
    save_product(state, 0, 'Dragon', 'Figures', price, '2', '5', 'Shelf A', '', 'None', None)


def test_upload_bytes_and_retire() -> None:
    """All supported file types retain binary bytes and editable metadata."""
    state = new_state()
    for extension in ('stl', '3mf', 'gcode'):
        data = b'\x00\xff\x01example\r\n'
        upload_file(state, DrafterBinaryFile('part.' + extension, data, '', len(data)),
                    'Models', 'v2', 'Printer A', True)
        assert_equal(state.files[-1].content, data, exact_strings=True)
    save_file_details(state, 1, 'Retired designs', 'v10', 'Old', False, retired=True)
    assert_equal(state.files[0].active, False, exact_strings=True)
    assert_equal(state.files[0].version, 'v10', exact_strings=True)
    assert_equal(isinstance(file_form(state, 1), Page), True, exact_strings=True)


def stock_list(state: State, kind: str) -> list[StockItem]:
    """Select a known inventory; reject forged inventory names."""
    if kind == "filament":
        return state.filaments
    if kind == "product":
        return state.products
    raise ValueError("Unknown inventory.")


def stock_card(item: StockItem, kind: str) -> Div:
    """Show stock prominently and provide explicit manual controls."""
    illustration = Image(item.image, alt=item.name) if item.image else Div("◈", classes="icon")
    return Div([illustration, Div(item.category, classes="tag"), Header(item.name, 3),
                Div(f"{item.quantity} in stock", classes="metric"),
                Text(f"Target {item.target}" + (f" · {money(item.price)}" if kind == "product" else " spools")),
                Div(item.lifecycle, classes="status-label"),
                Text("Low stock" if item.quantity < item.target else "Stock on target"),
                Button("− 1", change_stock, {"kind": kind, "uid": item.uid, "delta": -1}),
                Button("+ 1", change_stock, {"kind": kind, "uid": item.uid, "delta": 1}),
                Button("Details / edit", stock_form, {"kind": kind, "uid": item.uid})],
               classes="card " + item.lifecycle.lower())


@route
def filament(state: State,
                **unused: object) -> Page:
    """Show grouped spools and search all descriptive inventory fields."""
    cards = []
    for item in state.filaments:
        if matches(state.filament_query, f"{item.name} {item.location} {item.notes} {item.category}"):
            cards.append(stock_card(item, "filament"))
    return shell(state, "Filament inventory", [
        Button("Add filament", stock_form, {"kind": "filament", "uid": 0}),
        exchange_controls(state, "filaments"),
        Div([field("Search material, brand, color or location", TextBox("query", state.filament_query)),
             Button("Search filament", search_filament)], classes="filters"),
        Div(cards or [Text("No matching filament. Add a spool type to begin.")], classes="cards"),
    ])


@route
def search_filament(state: State, query: str,
                **unused: object) -> Page:
    """Remember the filament search."""
    state.filament_query, state.message = query, ""
    return filament(state)


@route
def products(state: State,
                **unused: object) -> Page:
    """Display product tiles, category choices and exact selling-price filters."""
    categories = ["All"]
    prices = ["All"]
    cards = []
    for item in state.products:
        if item.category not in categories:
            categories.append(item.category)
        if money(item.price) not in prices:
            prices.append(money(item.price))
        if (matches(state.product_query, f"{item.name} {item.category} {item.notes} {item.location}")
                and state.product_category in ("All", item.category)
                and state.product_price in ("All", money(item.price))):
            cards.append(stock_card(item, "product"))
    if state.product_category not in categories:
        categories.append(state.product_category)
    if state.product_price not in prices:
        prices.append(state.product_price)
    return shell(state, "Products & consumables", [
        Button("Add product", stock_form, {"kind": "product", "uid": 0}),
        exchange_controls(state, "products"),
        Div([field("Search products", TextBox("query", state.product_query)),
             field("Category", SelectBox("category", categories, state.product_category)),
             field("Selling price", SelectBox("price", prices, state.product_price)),
             Button("Filter products", search_products)], classes="filters"),
        Div(cards or [Text("No matching products. Add a product or clear the filters.")], classes="cards"),
    ])


@route
def search_products(state: State, query: str, category: str, price: str,
                **unused: object) -> Page:
    """Combine product text, category and price filters."""
    state.product_query, state.product_category, state.product_price = query, category, price
    state.message = ""
    return products(state)

def test_search_routes() -> None:
    state = new_state()
    search_files(state, 'v2', 'Retired')
    search_filament(state, 'PLA')
    search_products(state, 'dragon', 'Figures', '$25.00')
    search_budget(state, 'license')
    assert_equal(state.file_query, 'v2', exact_strings=True)
    assert_equal(state.file_status, 'Retired', exact_strings=True)
    assert_equal(state.filament_query, 'PLA', exact_strings=True)
    assert_equal(state.product_category, 'Figures', exact_strings=True)
    assert_equal(state.product_price, '$25.00', exact_strings=True)
    assert_equal(state.budget_query, 'license', exact_strings=True)


def find_stock(state: State, kind: str, uid: int) -> StockItem | None:
    """Find a stock record by identifier."""
    for item in stock_list(state, kind):
        if item.uid == uid:
            return item
    return None


@route
def stock_form(state: State, kind: str, uid: int, **unused: object) -> Page:
    """Show a form for one inventory record, with optional picture upload."""
    item = find_stock(state, kind, uid)
    if uid and item is None:
        state.message = "That inventory record no longer exists."
        return index(state)
    content = []
    if kind == "filament":
        content.extend([field("Brand", TextBox("brand", item.brand if item else "")),
                        field("Material", preset("material", ["PLA", "PETG", "ABS", "ASA", "TPU", "Nylon", "Other"],
                                                  item.material if item else "PLA")),
                        field("Color", TextBox("color", item.color if item else ""))])
    else:
        content.extend([field("Product name", TextBox("name", item.name if item else "")),
                        field("Selling price ($)", TextBox("price",
                              f"{item.price / 100:.2f}" if item else "0.00",
                              kind="number", min=0, step="0.01"))])
        choices = ["None"]
        selected = "None"
        for model in state.files:
            label = f"{model.uid}: {model.filename} ({model.version})"
            choices.append(label)
            if item and item.file_id == model.uid:
                selected = label
        content.append(field("Linked model version", SelectBox("linked_file", choices, selected)))
    if kind == "product":
        content.append(field("Category", preset("category", ["Figures", "Fidgets", "Decor", "Tools",
                                                              "Consumables", "Other"],
                                                item.category if item else "Figures")))
        content.extend(lifecycle_fields(item.lifecycle if item else "Active"))
    content.extend([
        field("Current stock", TextBox("quantity", str(item.quantity) if item else "0", kind="number", min=0, step=1)),
        field("Target stock", TextBox("target", str(item.target) if item else "0", kind="number", min=0, step=1)),
        field("Notes / drying status / details", TextArea("notes", item.notes if item else "")),
        field("Optional PNG or JPEG photo (up to 2 MiB)", FileUpload("photo", accept=".png,.jpg,.jpeg")),
        Text("Leave the photo empty to keep the existing picture."),
        Button("Save inventory", save_filament if kind == "filament" else save_product, {"uid": uid}),
    ])
    if item:
        content.append(Button("Remove catalog entry…", confirm_remove, {"kind": kind, "uid": uid}))
    return shell(state, ("Edit " if item else "Add ") + kind, [Div(content, classes="card")])


def photo_data(photo: DrafterBinaryFile | None, previous: str) -> str:
    """Encode optional raster photos for display without external hosting.

    Base64 is an advanced encoding that embeds bytes in an image URL.
    Only PNG/JPEG signatures are accepted; SVG/HTML content is not embedded.
    """
    if photo is None or (not photo.filename and not photo.content):
        return previous
    if not photo.content or len(photo.content) > 2 * 1024 * 1024:
        raise ValueError("Photos must be nonempty and no larger than 2 MiB.")
    if photo.content.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif photo.content.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    else:
        raise ValueError("Choose a PNG or JPEG photo.")
    # Base64 represents the photo bytes with printable characters. decode converts those
    # characters to a Python string that Drafter can use as the image address.
    return "data:" + mime + ";base64," + base64.b64encode(photo.content).decode("ascii")


@route
def save_filament(state: State, uid: int, brand: str, material: str, color: str,
                  category: str = "Filament", quantity: str = "0", target: str = "0", location: str = "",
                  notes: str = "", photo: DrafterBinaryFile | None = None,
                **unused: object) -> Page:
    """Add grouped spools or edit one type; duplicate additions increase stock."""
    remember_form(state, "stock", locals())
    try:
        if not all(value.strip() for value in (brand, material, color, category)):
            raise ValueError("Brand, material, color and category are required.")
        count, desired = whole_number(quantity), whole_number(target)
        existing = find_stock(state, "filament", uid)
        if uid and existing is None:
            raise ValueError("That filament record no longer exists.")
        picture = photo_data(photo, existing.image if existing else "")
        # Use the same spelling rules for all three parts of a spool type so identical spools group together.
        key = (brand.strip().casefold(), material.strip().casefold(), color.strip().casefold())
        for other in state.filaments:
            if (other.brand.casefold(), other.material.casefold(), other.color.casefold()) == key:
                if other.uid != uid:
                    if uid:
                        raise ValueError("That spool type already exists; adjust its quantity instead.")
                    other.quantity += count
                    state.message = "Matching spool type found; quantity added to its existing entry."
                    return filament(state)
        replacement = StockItem(uid or allocate_id(state), f"{brand.strip()} {material.strip()} {color.strip()}",
                                category.strip(), count, desired, 0, brand.strip(), material.strip(),
                                color.strip(), location.strip(), notes.strip(), picture, 0)
        replace_stock(state.filaments, replacement)
        state.message = "Filament inventory saved."
        return filament(state)
    except ValueError as error:
        state.message = str(error)
        return stock_form(state, "filament", uid)


def test_filament_grouping_and_edit() -> None:
    """Matching spool types merge case-insensitively; edits set counts."""
    state = new_state()
    save_filament(state, 0, 'Bambu', 'PLA', 'Black', 'Filament', '2', '5', 'A', '', None)
    save_filament(state, 0, ' bambu ', 'pla', 'black', 'Filament', '3', '5', 'B', '', None)
    assert_equal(len(state.filaments), 1, exact_strings=True)
    assert_equal(state.filaments[0].quantity, 5, exact_strings=True)
    save_filament(state, 1, 'Bambu', 'PLA', 'Black', 'Filament', '4', '6', 'A', 'Dry', None)
    assert_equal(state.filaments[0].quantity, 4, exact_strings=True)
    assert_equal(state.filaments[0].notes, 'Dry', exact_strings=True)


def replace_stock(items: list[StockItem], replacement: StockItem) -> None:
    """Replace an existing record, or append a newly allocated record."""
    for position in range(len(items)):
        if items[position].uid == replacement.uid:
            items[position] = replacement
            return
    items.append(replacement)


@route
def save_product(state: State, uid: int, name: str, category: str,
                 price: str, quantity: str, target: str, location: str = "",
                 notes: str = "", linked_file: str = "None", photo: DrafterBinaryFile | None = None,
                 active: bool = True, retired: bool = False, discontinued: bool = False, testing: bool = False,
                **unused: object) -> Page:
    """Save a product tile without linking stock changes to money."""
    remember_form(state, "stock", locals())
    try:
        if not name.strip() or not category.strip() or category.strip() == "All":
            raise ValueError("Enter a product name and category (other than All).")
        count, desired, cents = whole_number(quantity), whole_number(target), parse_money(price)
        existing = find_stock(state, "product", uid)
        if uid and existing is None:
            raise ValueError("That product no longer exists.")
        picture = photo_data(photo, existing.image if existing else "")
        file_id = 0 if linked_file == "None" else int(linked_file.split(":")[0])
        if file_id and get_file(state, file_id) is None:
            raise ValueError("Choose an existing model file.")
        lifecycle = chosen_lifecycle(active, retired, discontinued, testing)
        replacement = StockItem(uid or allocate_id(state), name.strip(), category.strip(), count,
                                desired, cents, "", "", "", location.strip(), notes.strip(), picture, file_id)
        replacement.lifecycle = lifecycle
        replace_stock(state.products, replacement)
        state.message = "Product inventory saved."
        return products(state)
    except ValueError as error:
        state.message = str(error)
        return stock_form(state, "product", uid)


def test_empty_optional_photo() -> None:
    """Browser empty-file placeholders behave like no optional photo."""
    state = new_state()
    blank = DrafterBinaryFile('', b'', 'application/octet-stream', 0)
    save_product(state, 0, 'Dragon', 'Figures', '25', '3', '8', '', '', 'None', blank)
    assert_equal(state.products[0].quantity, 3, exact_strings=True)
    assert_equal(state.products[0].image, '', exact_strings=True)


@route
def change_stock(state: State, kind: str, uid: int, delta: int,
                **unused: object) -> Page:
    """Apply only a one-unit adjustment; never permit negative inventory."""
    item = find_stock(state, kind, uid)
    if item is None or delta not in (-1, 1):
        state.message = "Select an existing item and a +1 or −1 adjustment."
    elif item.quantity + delta < 0:
        state.message = "Stock is already zero. The catalog entry is kept for restocking."
    else:
        item.quantity += delta
        state.message = f"{item.name}: stock is now {item.quantity}."
    return filament(state) if kind == "filament" else products(state)


@route
def confirm_remove(state: State, kind: str, uid: int,
                **unused: object) -> Page:
    """Require an explicit separate action before removing a catalog entry."""
    item = find_stock(state, kind, uid)
    if item is None:
        state.message = "That entry no longer exists."
        return index(state)
    return shell(state, "Remove catalog entry?", [
        Text(f"Remove {item.name}, currently {item.quantity} in stock? This removes its entire record."),
        Button("Yes, remove entry", remove_stock, {"kind": kind, "uid": uid}),
        Button("Keep entry", stock_form, {"kind": kind, "uid": uid}),
    ])


@route
def remove_stock(state: State, kind: str, uid: int, **unused: object) -> Page:
    """Remove the confirmed catalog entry without changing budget records."""
    items = stock_list(state, kind)
    for position in range(len(items)):
        if items[position].uid == uid:
            items.pop(position)
            break
    state.message = "Catalog entry removed."
    return filament(state) if kind == "filament" else products(state)


def test_plan_setup(state: State, due: str='2026-01-31') -> None:
    """Create a monthly commitment through its real route."""
    save_entry(state, 0, 'License', 'Licensing', '30.00', 'Expense', 'Planned',
               'Monthly', due, '', 'Designer', 'Commercial license', True)


def test_inventory_adjustment_and_removal() -> None:
    """Both inventories support adjustments; zero never becomes negative."""
    state = new_state()
    test_product_setup(state)
    change_stock(state, 'product', 1, -1)
    change_stock(state, 'product', 1, -1)
    change_stock(state, 'product', 1, -1)
    assert_equal(state.products[0].quantity, 0, exact_strings=True)
    change_stock(state, 'product', 1, 1)
    assert_equal(state.products[0].quantity, 1, exact_strings=True)
    assert_equal(state.entries, [], exact_strings=True)
    assert_equal(isinstance(confirm_remove(state, 'product', 1), Page), True, exact_strings=True)
    remove_stock(state, 'product', 1)
    assert_equal(state.products, [], exact_strings=True)


def recurring_monthly(state: State) -> int:
    """Estimate monthly expense commitments active on the report date.

    Annual amounts are divided by 12 for comparison, not recorded as payments.
    The due date is the next occurrence, so this is an active-commitment
    run rate, not an accrual report or a sum of payments already made.
    """
    annual = 0
    for entry in state.entries:
        if (entry.active and entry.status == "Planned" and entry.kind == "Expense"
                and (not entry.end or entry.end >= state.report_date)):
            if entry.frequency == "Monthly":
                annual += entry.amount * 12
            elif entry.frequency == "Yearly":
                annual += entry.amount
    return (annual + 6) // 12


@route
def budget(state: State, **unused: object) -> Page:
    """Separate real cash movements from planned expenses and subscriptions."""
    income, expenses, balance = totals(state)
    cards = []
    # Sort by due date, then record ID so entries on the same day have a stable order.
    # lambda defines the small function that sorted calls on each entry. Python
    # compares the first value in its tuple, then the second when the first values match.
    for entry in sorted(state.entries, key=lambda value: (value.due, value.uid)):
        if matches(state.budget_query, f"{entry.description} {entry.category} {entry.party} {entry.notes}"):
            details = [Div(f"{entry.kind} · {entry.status} · {entry.frequency}", classes="tag"),
                       Header(entry.description, 3), Div(money(entry.amount), classes="metric"),
                       Text(f"Date / next due: {entry.due} · {entry.category}"),
                       Text(f"Vendor / source: {entry.party}"), Text(entry.notes),
                       Text("Active" if entry.active else "Stopped / completed"),
                       Button("Edit", entry_form, {"uid": entry.uid})]
            if entry.status == "Planned" and entry.active:
                details.append(Button("Record payment / receipt", pay_form, {"uid": entry.uid}))
            cards.append(Div(details, classes="card"))
    return shell(state, "Budget & cash flow", [
        Text(f"Actual totals through {state.report_date}; totals include all records, regardless of search."),
        Div([metric("Received income", money(income)), metric("Paid expenses", money(expenses)),
             metric("Net cash flow", money(income - expenses)), metric("Recorded balance", money(balance)),
             metric("Monthly recurring expense estimate", money(recurring_monthly(state)))], classes="cards"),
        Text("Planned bills do not reduce the balance until recorded as paid. "
             "This is cash flow, not accounting profit."),
        Button("Add income / cost", entry_form, {"uid": 0}),
        exchange_controls(state, "budget"), monthly_chart(state),
        Div([field("Search budget records", TextBox("query", state.budget_query)),
             Button("Search budget", search_budget)], classes="filters"),
        Div([field("Opening balance ($)", TextBox("opening", f"{state.opening_balance / 100:.2f}")),
             field("Report through date (YYYY-MM-DD)", DateInput("through", state.report_date)),
             Button("Update summary", budget_settings)], classes="card"),
        Div(cards or [Text("No matching budget records. Add your first income or cost.")], classes="cards"),
    ])


@route
def search_budget(state: State, query: str,
                **unused: object) -> Page:
    """Filter displayed financial records, leaving all-record totals intact."""
    state.budget_query, state.message = query, ""
    return budget(state)


@route
def budget_settings(state: State, opening: str, through: str,
                **unused: object) -> Page:
    """Update the starting balance and report date after validating both."""
    try:
        cents = parse_money(opening, signed=True)
        valid_date(through)
        state.opening_balance, state.report_date = cents, through
        state.message = "Budget summary updated."
    except ValueError as error:
        state.message = str(error)
    return budget(state)


def test_budget_actuals_and_settings() -> None:
    """Only paid transactions through the report date affect cash flow."""
    state = new_state()
    state.report_date = '2026-10-28'
    budget_settings(state, '100.00', '2026-10-28')
    save_entry(state, 0, 'Sale', 'Sales', '50', 'Income', 'Paid', 'Once', '2026-10-27', '', 'Customer', '', True)
    save_entry(state, 0, 'Bags', 'Supplies', '12.50', 'Expense', 'Paid', 'Once', '2026-10-28', '', 'Shop', '', True)
    test_plan_setup(state)
    assert_equal(totals(state), (5000, 1250, 13750), exact_strings=True)
    assert_equal(recurring_monthly(state), 3000, exact_strings=True)
    budget_settings(state, '0', 'bad-date')
    assert_equal(state.opening_balance, 10000, exact_strings=True)


def find_entry(state: State, uid: int) -> Entry | None:
    """Find a financial record by identifier."""
    for entry in state.entries:
        if entry.uid == uid:
            return entry
    return None


@route
def entry_form(state: State, uid: int,
                **unused: object) -> Page:
    """Display detailed financial inputs and explain recurring plans."""
    entry = find_entry(state, uid)
    if uid and entry is None:
        state.message = "That financial record no longer exists."
        return budget(state)
    selected_frequency = state.draft.get("frequency", entry.frequency if entry else "Once")
    return shell(state, "Edit budget record" if entry else "Add income or cost", [
        Div([
            Text("Use Paid for an actual payment/receipt. Recurring entries are Planned commitments; "
                 "record each payment separately using its payment button."),
            field("Description", TextBox("description", entry.description if entry else "")),
            field("Category", preset("category", ["Supplies", "Filament", "Equipment", "Licensing",
                                                   "Rent", "Utilities", "Sales", "Other"],
                                      entry.category if entry else "Supplies")),
            field("Amount ($)", TextBox("amount",
                  f"{entry.amount / 100:.2f}" if entry else "0.00", kind="number", min=0, step="0.01")),
            field("Type", SelectBox("kind", ["Expense", "Income"], entry.kind if entry else "Expense")),
            field("Status", SelectBox("status", ["Planned", "Paid"], entry.status if entry else "Planned")),
            field("Frequency", SelectBox("frequency", list(FREQUENCIES),
                                         selected_frequency)),
            Button("Update fields for frequency", entry_frequency, {"uid": uid}),
            field("Date / first or next due (YYYY-MM-DD)",
                  DateInput("due", entry.due if entry else state.report_date)),
            *([field("Optional end date (YYYY-MM-DD)",
                     DateInput("end", entry.end if entry and entry.end else None))]
              if selected_frequency != "Once" else []),
            field("Vendor / income source", TextBox("party", entry.party if entry else "")),
            field("Notes", TextArea("notes", entry.notes if entry else "")),
            field("Active commitment", CheckBox("active", entry.active if entry else True)),
            Button("Save budget record", save_entry, {"uid": uid}),
        ], classes="card"),
    ])


@route
def save_entry(state: State, uid: int, description: str, category: str, amount: str,
               kind: str, status: str, frequency: str, due: str, end: str = "",
               party: str = "", notes: str = "", active: bool = True, retired: bool = False,
                discontinued: bool = False, testing: bool = False,
                **unused: object) -> Page:
    """Validate and save a transaction or a recurring plan atomically."""
    remember_form(state, "entry", locals())
    try:
        if not description.strip() or not category.strip():
            raise ValueError("Description and category are required.")
        cents = parse_money(amount)
        if cents <= 0:
            raise ValueError("Enter an amount greater than zero.")
        day = valid_date(due)
        if end and valid_date(end) < day:
            raise ValueError("End date cannot be before the due date.")
        if kind not in ("Expense", "Income") or status not in ("Planned", "Paid"):
            raise ValueError("Choose a valid type and status.")
        if frequency not in FREQUENCIES:
            raise ValueError("Choose Once, Monthly or Yearly.")
        if frequency != "Once" and status != "Planned":
            raise ValueError("Recurring records must be Planned; record individual payments separately.")
        previous = find_entry(state, uid)
        if uid and previous is None:
            raise ValueError("That record no longer exists.")
        anchor_day, anchor_month = day.day, day.month
        if previous and previous.due == due and previous.frequency == frequency:
            anchor_day, anchor_month = previous.anchor_day, previous.anchor_month
        replacement = Entry(uid or allocate_id(state), description.strip(), category.strip(), cents,
                            kind, status, frequency, due, end, party.strip(), notes.strip(), active,
                            anchor_day, anchor_month)
        for position in range(len(state.entries)):
            if state.entries[position].uid == uid:
                state.entries[position] = replacement
                state.message = "Budget record updated."
                return budget(state)
        state.entries.append(replacement)
        state.message = "Budget record added."
        return budget(state)
    except ValueError as error:
        state.message = str(error)
        return entry_form(state, uid)


def test_budget_edit_and_validation() -> None:
    """Edit existing amounts; reject impossible dates and paid recurrence."""
    state = new_state()
    state.report_date = '2026-10-28'
    test_plan_setup(state)
    save_entry(state, 1, 'License', 'Licensing', '40', 'Expense', 'Planned',
               'Monthly', '2026-01-31', '', 'Designer', '', True)
    assert_equal(state.entries[0].amount, 4000, exact_strings=True)
    save_entry(state, 0, 'X', 'Y', '2', 'Expense', 'Paid', 'Monthly', '2026-10-28', '', '', '', True)
    save_entry(state, 0, 'X', 'Y', '2', 'Expense', 'Paid', 'Once', '2026-02-30', '', '', '', True)
    assert_equal(len(state.entries), 1, exact_strings=True)


def next_due(entry: Entry) -> str:
    """Advance the due date, preserving month-end and leap-day intentions.

    calendar.monthrange handles short months. The original anchor day avoids
    turning January 31 -> February 28 -> March 28; March returns to the 31st.
    """
    current = valid_date(entry.due)
    year, month = current.year, current.month
    if entry.frequency == "Monthly":
        month += 1
        if month == 13:
            year, month = year + 1, 1
    else:
        year, month = year + 1, entry.anchor_month
    if year > 9999:
        raise ValueError("The next due date is outside the supported date range.")
    # A January 31 bill falls on the last day of February, then returns to March 31.
    # monthrange returns (first weekday, number of days). Index 1 gives the last valid day.
    day = min(entry.anchor_day, monthrange(year, month)[1])
    return date(year, month, day).isoformat()


@route
def pay_form(state: State, uid: int, **unused: object) -> Page:
    """Ask for the actual paid amount/date before changing financial totals."""
    entry = find_entry(state, uid)
    if entry is None or entry.status != "Planned" or not entry.active:
        state.message = "Choose an active planned record."
        return budget(state)
    return shell(state, "Record payment or receipt", [
        Text(entry.description),
        field("Actual amount ($)", TextBox("amount", f"{entry.amount / 100:.2f}")),
        field("Actual payment date (YYYY-MM-DD)", DateInput("paid_date", state.report_date)),
        Button("Confirm recorded payment", record_payment, {"uid": uid, "expected_due": entry.due}),
    ])


@route
def record_payment(state: State, uid: int, expected_due: str, amount: str, paid_date: str, **unused: object) -> Page:
    """Record one occurrence and advance a recurring plan without double counting.

    expected_due guards against resubmitting an old payment form after the
    plan has advanced. The actual receipt remains a separate one-time record.
    """
    try:
        entry = find_entry(state, uid)
        if (entry is None or entry.status != "Planned" or not entry.active
                or entry.due != expected_due):
            raise ValueError("This occurrence was already recorded or the plan changed.")
        cents = parse_money(amount)
        actual = valid_date(paid_date)
        if cents <= 0:
            raise ValueError("Payment must be greater than zero.")
        if entry.frequency == "Once":
            entry.status, entry.amount, entry.due = "Paid", cents, actual.isoformat()
        else:
            following = next_due(entry)
            payment = Entry(allocate_id(state), entry.description, entry.category, cents,
                            entry.kind, "Paid", "Once", actual.isoformat(), "", entry.party,
                            f"Payment for plan #{entry.uid}, due {entry.due}. {entry.notes}",
                            False, actual.day, actual.month)
            state.entries.append(payment)
            entry.due = following
            if entry.end and following > entry.end:
                entry.active = False
        state.message = "Payment recorded. Inventory was not changed."
    except ValueError as error:
        state.message = str(error)
    return budget(state)


def test_recurring_payment_and_replay() -> None:
    """Payment advances month-end dates; stale submissions do not duplicate."""
    state = new_state()
    state.report_date = '2026-10-28'
    test_plan_setup(state)
    assert_equal(isinstance(pay_form(state, 1), Page), True, exact_strings=True)
    record_payment(state, 1, '2026-01-31', '30', '2026-01-31')
    assert_equal(state.entries[0].due, '2026-02-28', exact_strings=True)
    record_payment(state, 1, '2026-01-31', '30', '2026-01-31')
    assert_equal(len(state.entries), 2, exact_strings=True)
    record_payment(state, 1, '2026-02-28', '29', '2026-02-28')
    assert_equal(state.entries[0].due, '2026-03-31', exact_strings=True)
    assert_equal(totals(state)[1], 5900, exact_strings=True)


def test_once_payment_and_end_date() -> None:
    """One-time plans become actuals; recurring plans stop after end date."""
    state = new_state()
    state.report_date = '2026-10-28'
    save_entry(state, 0, 'Bags', 'Supplies', '10', 'Expense', 'Planned', 'Once', '2026-10-28', '', '', '', True)
    record_payment(state, 1, '2026-10-28', '11', '2026-10-28')
    assert_equal(len(state.entries), 1, exact_strings=True)
    assert_equal(totals(state)[1], 1100, exact_strings=True)
    save_entry(state, 0, 'Rent', 'Rent', '100', 'Expense', 'Planned',
               'Monthly', '2026-10-28', '2026-10-31', '', '', True)
    record_payment(state, 2, '2026-10-28', '100', '2026-10-28')
    assert_equal(state.entries[1].active, False, exact_strings=True)


def remember_form(state: State, form: str, values: dict) -> None:
    """Capture scalar fields for correction after a validation error.

    locals() supplies route arguments. Only strings/booleans/numbers are kept;
    browser file inputs must be selected again, as browsers require.
    """
    state.draft = {key: value for key, value in values.items()
                   if isinstance(value, (str, bool, int)) and key != "state"}
    state.draft["form"] = form


def restore_form(content: list, values: dict) -> None:
    """Walk nested form layout components to restore entered values."""
    for component in content:
        if isinstance(component, (list, tuple)):
            # Layouts can contain lists inside lists. This recursive call visits the next level.
            # Once it reaches an individual input, the matching branch restores its value.
            restore_form(component, values)
            continue
        if isinstance(component, (TextBox, TextArea, SelectBox, CheckBox, DateInput)):
            if component.name in values:
                value = values[component.name]
                if isinstance(component, SelectBox) and value not in component.options:
                    component.options.append(value)
                component.default_value = value
        if isinstance(component, Div) and isinstance(component.content, list):
            restore_form(component.content, values)


@route
def entry_frequency(state: State, uid: int, frequency: str, **values: object) -> Page:
    """Refresh recurrence fields while preserving the rest of the form."""
    remember_form(state, "entry", values)
    state.draft["frequency"] = frequency if frequency in FREQUENCIES else "Once"
    if frequency != "Once":
        state.draft["status"] = "Planned"
    return entry_form(state, uid)


def preset(name: str, options: list[str], current: str) -> SelectBox:
    """Offer common values while preserving an existing imported choice."""
    choices = options.copy()
    if current and current not in choices:
        choices.append(current)
    return SelectBox(name, choices, current or choices[0])


def lifecycle_fields(current: str) -> list:
    """Render explicit checkboxes; the save route rejects conflicting choices."""
    result = [Text("Status — select exactly one checkbox.")]
    for value in LIFECYCLES:
        result.append(field(value, CheckBox(value.lower(), current == value)))
    return result


def chosen_lifecycle(active: bool, retired: bool, discontinued: bool, testing: bool) -> str:
    """Require exactly one lifecycle choice."""
    selected = []
    # Pair each status label with its checkbox in the same order as the form.
    # zip takes one item from each sequence per step, such as ('Retired', True).
    for name, enabled in zip(LIFECYCLES, (active, retired, discontinued, testing)):
        if enabled:
            selected.append(name)
    if not selected:
        raise ValueError("Select one status checkbox before saving.")
    if len(selected) != 1:
        raise ValueError("Select only one status: Active, Retired, Discontinued or Testing.")
    return selected[0]


def test_status_and_form_preservation() -> None:
    """Conflicting statuses are rejected; correction keeps typed values."""
    state = new_state()
    rejected = False
    try:
        chosen_lifecycle(True, True, False, False)
    except ValueError:
        rejected = True
    assert_equal(rejected, True)
    save_product(state, 0, 'Keep my name', 'Figures', '-2', '2', '3')
    assert_equal(state.draft['name'], 'Keep my name', exact_strings=True)
    page = entry_frequency(state, 0, 'Monthly', description='Keep this', amount='30')
    assert_equal("TextBox('description', 'Keep this'" in str(page), True, exact_strings=True)
    assert_equal(state.draft['description'], 'Keep this', exact_strings=True)
    assert_equal(state.draft['status'], 'Planned', exact_strings=True)
    assert_equal(isinstance(monthly_chart(state), Div), True, exact_strings=True)


@route
def todos(state: State, **unused: object) -> Page:
    """Show a basic task list with completion controls."""
    content = [field("What needs to be done?", TextBox("task")), Button("Add task", add_todo),
               exchange_controls(state, "todos")]
    for task in state.todos:
        content.append(Div([Text(task.text),
                            Button("Reopen" if task.done else "Mark done", toggle_todo, {"uid": task.uid}),
                            Text("Completed" if task.done else "To do")], classes="card"))
    return shell(state, "To-do", content)


@route
def add_todo(state: State, task: str, **unused: object) -> Page:
    """Add a nonempty task."""
    if task.strip():
        state.todos.append(Todo(allocate_id(state), task.strip()))
        state.message = "Task added."
    else:
        state.message = "Enter a task first."
    return todos(state)


@route
def toggle_todo(state: State, uid: int, **unused: object) -> Page:
    """Complete or reopen the selected task."""
    for task in state.todos:
        if task.uid == uid:
            # The same button completes an open task or reopens a completed one.
            task.done = not task.done
    return todos(state)


def test_todos_and_export() -> None:
    """Tasks can be completed/reopened and exported to portable CSV."""
    state = new_state()
    assert_equal(isinstance(todos(state), Page), True, exact_strings=True)
    add_todo(state, 'Dry PLA')
    toggle_todo(state, 1)
    assert_equal(state.todos[0].done, True, exact_strings=True)
    data = csv_bytes('todos', csv_rows(state, 'todos'))
    other = new_state()
    preview_import(other, 'todos', DrafterBinaryFile('tasks.csv', data, '', len(data)), 'Replace counts')
    confirm_import(other)
    assert_equal(other.todos[0].text, 'Dry PLA', exact_strings=True)
    assert_equal(other.todos[0].done, True, exact_strings=True)


def csv_columns(kind: str) -> list[str]:
    """Define portable CSV schemas; model bytes and photos are separate assets."""
    schemas = {
        "filaments": "brand,material,color,quantity,target,notes",
        "products": "name,category,price,quantity,target,status,notes",
        "files": "filename,category,version,status,notes",
        "budget": "description,category,amount,kind,status,frequency,due,end,party,notes,active",
        "todos": "text,done",
    }
    if kind not in schemas:
        raise ValueError("Unknown data section.")
    return schemas[kind].split(",")


def csv_rows(state: State, kind: str) -> list[dict]:
    """Export all section records, regardless of current search filters."""
    records = state.entries if kind == "budget" else getattr(state, kind)
    result = []
    for item in records:
        row = {}
        for column in csv_columns(kind):
            if column == "status" and kind in ("files", "products"):
                value = item.lifecycle
            else:
                # Read the record field named by this CSV column.
                value = getattr(item, column)
            if column in ("price", "amount"):
                value = f"{value / 100:.2f}"
            row[column] = str(value)
        result.append(row)
    return result


def csv_bytes(kind: str, rows: list[dict]) -> bytes:
    """Use Python's CSV quoting for commas, newlines and Unicode.

    StringIO is an in-memory text file. Formula-looking text is prefixed with
    an apostrophe so spreadsheet programs do not execute imported content.
    """
    stream = io.StringIO(newline="")
    # DictWriter uses the column names as dictionary keys. It quotes fields containing
    # commas or newlines so they can be read back as a single cell.
    writer = csv.DictWriter(stream, fieldnames=csv_columns(kind))
    writer.writeheader()
    for row in rows:
        safe = {}
        for key, value in row.items():
            text = str(value)
            safe[key] = "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text
        writer.writerow(safe)
    return stream.getvalue().encode("utf-8-sig")


def exchange_controls(state: State, kind: str) -> Div:
    """Offer full-data export and a guided import."""
    return Div([Button("Import CSV", import_form, {"kind": kind}),
                Download("Download CSV", kind + ".csv", csv_bytes(kind, csv_rows(state, kind)),
                         content_type="text/csv;charset=utf-8")], classes="nav")


@route
def import_form(state: State, kind: str, **unused: object) -> Page:
    """Provide a schema template and an explicit duplicate-handling choice."""
    return shell(state, "Import " + kind, [
        Text("UTF-8 CSV, up to 1 MiB / 500 rows. Download the template for column names."),
        Text("Files CSV contains catalog details only; upload the actual model files separately."),
        Text("Inventory matches brand/material/color or product name. Files match filename + version. "
             "Budget and tasks append new records each time; importing them twice creates duplicates."),
        Text("Status: Active, Retired, Discontinued or Testing. Budget: Income/Expense, Planned/Paid, "
             "Once/Monthly/Yearly. Dates: YYYY-MM-DD. Booleans: true/false. Prices and amounts are dollars."),
        Text("CSV omits photos, model bytes, product-to-file links and opening balance. "
             "Other websites may require mapping these columns to their own template."),
        Download("Download CSV template", kind + "-template.csv", csv_bytes(kind, []), content_type="text/csv"),
        field("CSV file", FileUpload("upload", accept=".csv")),
        field("Existing inventory quantities", SelectBox("mode", ["Replace counts", "Add counts"])),
        Button("Preview import", preview_import, {"kind": kind}),
    ])


def csv_bool(value: str) -> bool:
    """Accept only clear boolean CSV values."""
    if value.casefold() not in ("true", "false", "yes", "no", "1", "0"):
        raise ValueError("Boolean values must be true/false, yes/no or 1/0.")
    return value.casefold() in ("true", "yes", "1")


def import_row(state: State, kind: str, row: dict, mode: str) -> None:
    """Validate one CSV row and apply it to a working copy of state."""
    if kind in ("filaments", "products"):
        quantity, target = whole_number(row["quantity"]), whole_number(row["target"])
        if kind == "filaments":
            if not all(row[key] for key in ("brand", "material", "color")):
                raise ValueError("Brand, material and color are required.")
            name = f'{row["brand"]} {row["material"]} {row["color"]}'
            items = state.filaments
            price, category, status = 0, "Filament", "Active"
        else:
            name, category, status = row["name"], row["category"], row["status"]
            price, items = parse_money(row["price"]), state.products
            if not name or not category or category == "All" or status not in LIFECYCLES:
                raise ValueError("Provide a name, category and valid lifecycle status.")
        old = None
        for item in items:
            if item.name.casefold() == name.casefold():
                old = item
        if old:
            # Add counts for a shipment. Replace counts for a full inventory recount.
            old.quantity = old.quantity + quantity if mode == "Add counts" else quantity
            old.target, old.price, old.category = target, price, category
            old.lifecycle, old.notes = status, row["notes"]
        else:
            items.append(StockItem(allocate_id(state), name, category, quantity, target, price,
                                   row.get("brand", ""), row.get("material", ""), row.get("color", ""),
                                   "", row["notes"], "", 0, status))
    elif kind == "files":
        if (not row["filename"].lower().endswith((".stl", ".3mf", ".gcode"))
                or not row["version"] or row["status"] not in LIFECYCLES):
            raise ValueError("Filename, version and valid status are required.")
        for item in state.files:
            if item.filename == row["filename"] and item.version == row["version"]:
                item.category, item.notes = row["category"], row["notes"]
                item.lifecycle, item.active = row["status"], row["status"] != "Retired"
                return
        state.files.append(ModelFile(allocate_id(state), row["filename"], b"", row["category"],
                                     row["version"], row["notes"], row["status"] != "Retired", row["status"]))
    elif kind == "budget":
        before = len(state.entries)
        save_entry(state, 0, row["description"], row["category"], row["amount"], row["kind"],
                   row["status"], row["frequency"], row["due"], row["end"], row["party"],
                   row["notes"], csv_bool(row["active"]))
        if len(state.entries) == before:
            raise ValueError(state.message)
    elif kind == "todos":
        if not row["text"]:
            raise ValueError("Task text is required.")
        state.todos.append(Todo(allocate_id(state), row["text"], csv_bool(row["done"])))


@route
def preview_import(state: State, kind: str, upload: DrafterBinaryFile | None,
                   mode: str, **unused: object) -> Page:
    """Validate a complete CSV on a deep copy before offering confirmation.

    deepcopy isolates nested records so failed imports cannot partially alter
    the real inventory. Only validated text rows are retained for confirmation.
    """
    state.pending_rows = []
    try:
        if not upload or not upload.content or len(upload.content) > 1024 * 1024:
            raise ValueError("Choose a nonempty CSV no larger than 1 MiB.")
        if mode not in ("Replace counts", "Add counts"):
            raise ValueError("Choose an inventory import mode.")
        # Excel sometimes adds a marker before the first column name. utf-8-sig consumes it.
        reader = csv.DictReader(io.StringIO(upload.content.decode("utf-8-sig")))
        if reader.fieldnames != csv_columns(kind):
            raise ValueError("Column names/order must match the downloaded template.")
        rows = list(reader)
        if not rows or len(rows) > 500:
            raise ValueError("Import between 1 and 500 data rows.")
        # deepcopy also copies the records inside each list. Updating a copied quantity
        # during preview therefore leaves the user's inventory untouched.
        working = deepcopy(state)
        # Row 1 contains column names, so the first error a user can fix is on row 2.
        for number, row in enumerate(rows, 2):
            if None in row or any(value is None for value in row.values()):
                raise ValueError(f"Row {number}: wrong number of columns.")
            row = {key: value.strip() for key, value in row.items()}
            rows[number - 2] = row
            try:
                import_row(working, kind, row, mode)
            except ValueError as error:
                raise ValueError(f"Row {number}: {error}") from None
        state.pending_rows, state.pending_kind, state.pending_mode = rows, kind, mode
        return shell(state, "Review CSV import", [
            Text(f"{len(rows)} valid rows · {kind} · {mode}. No data has changed yet."),
            *[Div([Text(f"{key}: {value}") for key, value in row.items()], classes="card") for row in rows[:10]],
            Text("Preview shows the first 10 rows; all rows were checked."),
            Button("Confirm import", confirm_import), Button("Cancel", cancel_import),
        ])
    except (ValueError, UnicodeError, csv.Error) as error:
        state.message = str(error)
        return import_form(state, kind)


@route
def confirm_import(state: State, **unused: object) -> Page:
    """Commit an already previewed batch once, then clear pending rows."""
    if not state.pending_rows:
        state.message = "No pending import."
        return index(state)
    working = deepcopy(state)
    try:
        for row in state.pending_rows:
            import_row(working, state.pending_kind, row, state.pending_mode)
    except ValueError as error:
        state.message = str(error)
        return index(state)
    state.files, state.filaments, state.products = working.files, working.filaments, working.products
    state.entries, state.todos, state.next_id = working.entries, working.todos, working.next_id
    state.message = f"Imported {len(state.pending_rows)} rows."
    state.pending_rows = []
    return index(state)


@route
def cancel_import(state: State, **unused: object) -> Page:
    """Discard the preview without changing records."""
    state.pending_rows = []
    state.message = "Import cancelled."
    return index(state)


def test_csv_inventory_preview_commit_and_cancel() -> None:
    """Preview never mutates stock; confirming is one-shot; cancel preserves data."""
    state = new_state()
    assert_equal(isinstance(import_form(state, 'products'), Page), True, exact_strings=True)
    data = csv_bytes('products', [dict(name='Dragon', category='Figures', price='25.00',
                    quantity='4', target='8', status='Testing', notes='Comma, and newline\nhere')])
    upload = DrafterBinaryFile('products.csv', data, 'text/csv', len(data))
    preview_import(state, 'products', upload, 'Replace counts')
    assert_equal(len(state.products), 0, exact_strings=True)
    assert_equal(len(state.pending_rows), 1, exact_strings=True)
    confirm_import(state)
    confirm_import(state)
    assert_equal(len(state.products), 1, exact_strings=True)
    assert_equal(state.products[0].quantity, 4, exact_strings=True)
    preview_import(state, 'products', upload, 'Add counts')
    confirm_import(state)
    assert_equal(state.products[0].quantity, 8, exact_strings=True)
    preview_import(state, 'products', upload, 'Replace counts')
    cancel_import(state)
    assert_equal(state.products[0].quantity, 8, exact_strings=True)


def test_csv_bad_row_is_atomic() -> None:
    """One invalid row prevents the entire batch being staged or committed."""
    state = new_state()
    rows = [dict(brand='Bambu', material='PLA', color='Black', quantity='3', target='5', notes=''),
            dict(brand='Bambu', material='PLA', color='White', quantity='-2', target='5', notes='')]
    data = csv_bytes('filaments', rows)
    preview_import(state, 'filaments', DrafterBinaryFile('a.csv', data, '', len(data)), 'Replace counts')
    assert_equal(state.filaments, [], exact_strings=True)
    assert_equal(state.pending_rows, [], exact_strings=True)
    assert_equal('Row 3' in state.message, True, exact_strings=True)


@route
def batch_file_form(state: State, **unused: object) -> Page:
    """Select several model files for a common initial version/category."""
    return shell(state, "Upload multiple models", [
        field("Model files", FileUpload("uploads", accept=".stl,.3mf,.gcode", multiple=True)),
        field("Category", preset("category", ["Models", "G-code", "Other"], "Models")),
        field("Version", TextBox("version", "v1")), Button("Upload selected files", batch_upload),
    ])


@route
def batch_upload(state: State, uploads: list[DrafterBinaryFile], category: str,
                 version: str, **unused: object) -> Page:
    """Upload a batch atomically using the existing individual validation."""
    working = deepcopy(state)
    for upload in uploads:
        before = sum(len(item.content) for item in working.files)
        upload_file(working, upload, category, version, "", True)
        if sum(len(item.content) for item in working.files) == before:
            state.message = working.message
            return batch_file_form(state)
    state.files, state.next_id = working.files, working.next_id
    state.message = f"Uploaded {len(uploads)} files."
    return files(state)


def test_batch_files_and_catalog_attach() -> None:
    """Batch uploads validate atomically; catalog placeholders gain real bytes."""
    state = new_state()
    assert_equal(isinstance(batch_file_form(state), Page), True, exact_strings=True)
    batch_upload(state, [DrafterBinaryFile('a.stl', b'abc', '', 3),
                         DrafterBinaryFile('bad.exe', b'bad', '', 3)], 'Models', 'v1')
    assert_equal(state.files, [], exact_strings=True)
    import_row(state, 'files', dict(filename='a.stl', category='Models', version='v1',
                                   status='Active', notes=''), 'Replace counts')
    batch_upload(state, [DrafterBinaryFile('a.stl', b'abc', '', 3)], 'Models', 'v1')
    assert_equal(len(state.files), 1, exact_strings=True)
    assert_equal(state.files[0].content, b'abc', exact_strings=True)


def monthly_chart(state: State) -> Div:
    """Render a six-month cash-flow chart with text labels and exact totals."""
    report_day = valid_date(state.report_date)
    year, month = report_day.year, report_day.month
    rows = []
    largest = 1
    for offset in range(5, -1, -1):
        absolute = year * 12 + month - 1 - offset
        prefix = f"{absolute // 12:04d}-{absolute % 12 + 1:02d}"
        income, expense = 0, 0
        for entry in state.entries:
            if entry.status == "Paid" and entry.due.startswith(prefix) and entry.due <= state.report_date:
                if entry.kind == "Income":
                    income += entry.amount
                else:
                    expense += entry.amount
        largest = max(largest, income, expense)
        rows.append((prefix, income, expense))
    content = [Header("Monthly income vs. paid costs", 2)]
    # Use one scale across all six months so bar lengths can be compared.
    for month_name, income, expense in rows:
        content.extend([Text(f"{month_name} · Income {money(income)} · Costs {money(expense)}"),
                        Div("", classes="bar", width=f"{income / largest * 100}%"),
                        Div("", classes="bar expense-bar", width=f"{expense / largest * 100}%")])
    return Div(content, classes="card")


# The CSS below styles the cards and status colors used on the pages above.
STYLE = """
body { background:#f3f5f2; color:#19342e; font-family:Segoe UI, sans-serif; }
.hub { color:#172c46; background:#f5f8fc;
 max-width:1180px; margin:24px auto; padding:16px; font-size:16px; line-height:1.5; }
.hub .card { border-top:5px solid #168a86; }
.hub .retired { border-color:#8290a3; background:#edf0f5; }
.hub .discontinued { border-color:#be4551; background:#fff0f1; }
.hub .testing { border-color:#c4911b; background:#fff8dd; }
.status-label { font-weight:700; margin:8px 0; }
.bar { height:16px; background:#168a86; border-radius:3px; min-width:1px; }
.expense-bar { background:#bb5665; }
.hub h1 { font-size:32px; margin:18px 0; }
.hub h2 { font-size:23px; }
.hub h3 { font-size:20px; }
.brand { font-size:13px; letter-spacing:3px; color:#52786b; font-weight:700; }
.nav { display:flex; flex-wrap:wrap; gap:8px; margin:20px 0; }
.hub button { background:#126b78; box-shadow:none; color:white; border:0; border-radius:8px;
 padding:10px 16px; cursor:pointer; font-weight:600; margin:4px; }
.hub button:hover { background:#105b66; }
button:focus-visible, input:focus-visible, select:focus-visible {
 outline:3px solid #dfab45; outline-offset:2px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:16px; }
.card { background:white; border:1px solid #dce5df; border-radius:14px;
 padding:20px; margin-bottom:14px; box-shadow:0 3px 9px #18362b08; }
.card img { width:100%; height:150px; object-fit:contain; border-radius:8px; }
.metric { font-size:28px; font-weight:700; color:#1c5847; margin:8px 0; }
.muted { color:#5e7067; font-size:14px; }
.notice { background:#fff0ce; padding:12px 16px; border-radius:8px; margin:12px 0; }
.message { background:#dceee3; padding:12px 16px; border-radius:8px; }
.field { margin:12px 0; }
.field input:not([type=checkbox]), .field select, .field textarea {
 display:block; width:100%; box-sizing:border-box; max-width:620px; padding:10px;
 border:1px solid #adbbb1; border-radius:6px; background:white; color:#19342e; }
.filters { padding:16px; background:#e6ece6; border-radius:12px; margin:16px 0; }
.tag { color:#52786b; font-size:13px; text-transform:uppercase; letter-spacing:1px; }
.icon { font-size:48px; padding:20px; background:#edf3ed; border-radius:8px; }
h1,h2,h3 { color:#19342e; }
@media(max-width:600px) { .hub { margin:0; padding:10px; } .cards { grid-template-columns:1fr; } }
"""


def run_tests() -> None:
    """Run all Bakery tests without starting the website."""
    from bakery import student_tests
    # Bakery counts failures as comparisons run. Remember the starting count so the
    # final check reports failures from this run, even if Python already ran other tests.
    failures_before = student_tests.failures
    test_pages_and_forms()
    test_search_routes()
    test_upload_bytes_and_retire()
    test_filament_grouping_and_edit()
    test_inventory_adjustment_and_removal()
    test_empty_optional_photo()
    test_budget_actuals_and_settings()
    test_recurring_payment_and_replay()
    test_once_payment_and_end_date()
    test_budget_edit_and_validation()
    test_csv_inventory_preview_commit_and_cancel()
    test_csv_bad_row_is_atomic()
    test_todos_and_export()
    test_batch_files_and_catalog_attach()
    test_status_and_form_preservation()
    test_helper_examples()
    # Bakery reports each comparison. A failing exit status also flags it in PyCharm.
    if student_tests.failures > failures_before:
        raise SystemExit(1)
    print("All Bakery test groups passed.")


if __name__ == "__main__":
    # Run the examples once per launch. Each test creates its own sample State.
    run_tests()
    # sys.argv contains the options passed to Python. --test finishes after the checks.
    if "--test" not in sys.argv:
        set_website_title("Tanka Hub")
        add_website_css(STYLE)
        if sys.platform != "emscripten":
            set_site_information("Tanka Hub", "A 3D printing shop workspace.",
                                 ["Drafter", "Bakery"], "PROJECT_SCOPE.md", [])
        start_server(new_state())
