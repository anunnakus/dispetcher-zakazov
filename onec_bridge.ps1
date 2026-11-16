#requires -Version 5.0
$ErrorActionPreference = "Stop"
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ProgressPreference = "SilentlyContinue"

function Write-Answer($obj) {
  $json = $obj | ConvertTo-Json -Compress -Depth 6
  [Console]::Out.WriteLine($json)
}

function Escape-1C([string]$value) {
  if ([string]::IsNullOrEmpty($value)) { return "" }
  return ($value -replace '"', '""')
}

function Connect-OneC($cmd) {
  $connector = New-Object -ComObject "V83.COMConnector"
  $parts = @()
  if ($cmd.file) {
    $parts += ('File="{0}"' -f (Escape-1C $cmd.file))
  } elseif ($cmd.server -and $cmd.ref) {
    $parts += ('Srvr="{0}";Ref="{1}"' -f (Escape-1C $cmd.server), (Escape-1C $cmd.ref))
  } else {
    throw "Укажите папку файловой базы или сервер и имя базы"
  }
  if ($cmd.user) { $parts += ('Usr="{0}"' -f (Escape-1C $cmd.user)) }
  if ($cmd.password) { $parts += ('Pwd="{0}"' -f (Escape-1C $cmd.password)) }
  return $connector.Connect(($parts -join ";"))
}

function New-OneCObject($conn, $names) {
  foreach ($name in $names) {
    try { return $conn.NewObject($name) } catch {}
  }
  throw "Не удалось создать объект 1С"
}

function Set-IfExists($obj, $name, $value) {
  try { $obj.$name = $value; return $true } catch { return $false }
}

function Invoke-OrderQuery($conn, $text, $days) {
  $query = New-OneCObject $conn @("Query", "Запрос")
  $limitDate = (Get-Date).Date.AddDays(-1 * [int]$days)
  $literal = "ДАТАВРЕМЯ({0},{1},{2})" -f $limitDate.Year, $limitDate.Month, $limitDate.Day
  $query.Text = $text.Replace("&Since", $literal)
  $selection = $query.Execute().Choose()
  $rows = @()
  while ($selection.Next()) {
    $moment = ""
    try { $moment = ([datetime]$selection.OrderDate).ToString("yyyy-MM-ddTHH:mm:ss") } catch {}
    $rows += [ordered]@{
      number = [string]$selection.OrderNumber
      created_at = $moment
      customer = [string]$selection.Customer
      phone = ""
      email = ""
      amount = [string]$selection.Amount
      status = [string]$selection.StatusName
      address = [string]$selection.Address
      comment = [string]$selection.Comment
      items = ""
      track = ""
    }
  }
  return $rows
}

function Get-Orders($conn, $days) {
  $queries = @(
@{ name = "ЗаказКлиента"; text = @'
ВЫБРАТЬ ПЕРВЫЕ 200
    Заказ.Номер КАК OrderNumber,
    Заказ.Дата КАК OrderDate,
    ПРЕДСТАВЛЕНИЕ(Заказ.Контрагент) КАК Customer,
    Заказ.СуммаДокумента КАК Amount,
    Заказ.Комментарий КАК Comment,
    ПРЕДСТАВЛЕНИЕ(Заказ.Статус) КАК StatusName,
    Заказ.АдресДоставки КАК Address
ИЗ
    Документ.ЗаказКлиента КАК Заказ
ГДЕ
    НЕ Заказ.ПометкаУдаления
    И Заказ.Дата >= &Since
УПОРЯДОЧИТЬ ПО
    Заказ.Дата УБЫВ
'@ },
@{ name = "ЗаказПокупателя"; text = @'
ВЫБРАТЬ ПЕРВЫЕ 200
    Заказ.Номер КАК OrderNumber,
    Заказ.Дата КАК OrderDate,
    ПРЕДСТАВЛЕНИЕ(Заказ.Контрагент) КАК Customer,
    Заказ.СуммаДокумента КАК Amount,
    Заказ.Комментарий КАК Comment,
    ПРЕДСТАВЛЕНИЕ(Заказ.СостояниеЗаказа) КАК StatusName,
    "" КАК Address
ИЗ
    Документ.ЗаказПокупателя КАК Заказ
ГДЕ
    НЕ Заказ.ПометкаУдаления
    И Заказ.Дата >= &Since
УПОРЯДОЧИТЬ ПО
    Заказ.Дата УБЫВ
'@ }
  )
  $errors = @()
  foreach ($item in $queries) {
    try {
      return @{ used = $item.name; orders = (Invoke-OrderQuery $conn $item.text $days) }
    } catch {
      $errors += ("{0}: {1}" -f $item.name, $_.Exception.Message)
    }
  }
  throw ("Не нашёл заказы покупателей в 1С. " + ($errors -join " | "))
}

function Set-OutgoingState($conn, $obj) {
  foreach ($enumName in @("СостоянияДокументаСообщениеSMS", "СостоянияСообщенияSMS")) {
    try { $enum = $conn.Enums.$enumName } catch { continue }
    foreach ($valueName in @("Исходящее", "Черновик")) {
      try { $obj.Состояние = $enum.$valueName; return } catch {}
    }
  }
}

function Send-Sms($conn, $cmd) {
  $manager = $conn.Documents.СообщениеSMS
  $doc = $manager.CreateDocument()
  $doc.Date = Get-Date
  Set-IfExists $doc "Дата" (Get-Date) | Out-Null
  if (-not (Set-IfExists $doc "ТекстСообщения" $cmd.text)) {
    Set-IfExists $doc "Текст" $cmd.text | Out-Null
  }
  Set-IfExists $doc "Комментарий" $cmd.subject | Out-Null
  try {
    $row = $doc.Адресаты.Add()
    Set-IfExists $row "НомерДляОтправки" $cmd.phone | Out-Null
    Set-IfExists $row "КакСвязаться" $cmd.phone | Out-Null
    Set-IfExists $row "ПредставлениеКонтакта" $cmd.customer | Out-Null
  } catch {}
  Set-OutgoingState $conn $doc
  $doc.Write()
  return "1С приняла SMS. Его отправит учётная запись SMS в 1С."
}

function Send-Mail($conn, $cmd) {
  if (-not $cmd.email) { throw "В заказе нет почты, а документ SMS в 1С не найден" }
  $accounts = $conn.Catalogs.УчетныеЗаписиЭлектроннойПочты.Select()
  if (-not $accounts.Next()) { throw "В 1С нет учётной записи почты" }
  $doc = $conn.Documents.ЭлектронноеПисьмоИсходящее.CreateDocument()
  Set-IfExists $doc "Дата" (Get-Date) | Out-Null
  Set-IfExists $doc "Date" (Get-Date) | Out-Null
  $doc.УчетнаяЗапись = $accounts.Ref
  Set-IfExists $doc "Тема" $cmd.subject | Out-Null
  if (-not (Set-IfExists $doc "Текст" $cmd.text)) {
    Set-IfExists $doc "ТекстПисьма" $cmd.text | Out-Null
  }
  try {
    $row = $doc.Получатели.Add()
    Set-IfExists $row "Адрес" $cmd.email | Out-Null
    Set-IfExists $row "Представление" $cmd.customer | Out-Null
  } catch {}
  $doc.Write()
  return "1С приняла письмо. Его отправит учётная запись почты в 1С."
}

function Send-OneC($conn, $cmd) {
  if ($cmd.phone) {
    try { return Send-Sms $conn $cmd } catch {}
  }
  return Send-Mail $conn $cmd
}

try {
  $raw = [Console]::In.ReadToEnd()
  $cmd = $raw | ConvertFrom-Json
  $conn = Connect-OneC $cmd
  if ($cmd.action -eq "ping") {
    $title = ""
    try { $title = [string]$conn.Metadata.Name } catch {}
    if (-not $title) { $title = "база открыта" }
    Write-Answer @{ ok = $true; message = "База 1С открылась: $title" }
    exit 0
  }
  if ($cmd.action -eq "pull") {
    $days = 7
    if ($cmd.days) { $days = [int]$cmd.days }
    $found = Get-Orders $conn $days
    Write-Answer @{ ok = $true; used = $found.used; orders = @($found.orders) }
    exit 0
  }
  if ($cmd.action -eq "send") {
    $message = Send-OneC $conn $cmd
    Write-Answer @{ ok = $true; message = $message }
    exit 0
  }
  throw "Неизвестная команда"
} catch {
  Write-Answer @{ ok = $false; error = $_.Exception.Message }
  exit 1
}
