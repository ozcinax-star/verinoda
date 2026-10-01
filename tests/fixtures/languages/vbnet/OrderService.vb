Imports System.Collections.Generic

Namespace Shop
    Public Partial Class OrderService
        Public Function PlaceOrder(items As List(Of String)) As Integer
            Dim total As Integer = CountItems(items)
            Validate(total)
            Return total
        End Function

        Private Function CountItems(items As List(Of String)) As Integer
            Return items.Count
        End Function
    End Class
End Namespace
