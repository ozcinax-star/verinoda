Namespace Shop
    Partial Public Class OrderService
        Private Sub Validate(total As Integer)
            If total < 0 Then
                Throw New System.ArgumentException("negative total")
            End If
        End Sub
    End Class
End Namespace
